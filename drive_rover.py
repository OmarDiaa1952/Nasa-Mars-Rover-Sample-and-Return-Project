"""
Main module for Mars Search Robot.

Gets rover telemetry data and supervises core tasks of
autonomous navigation and mapping

"""



# Standard library imports
import os
import time
import json
import base64
import shutil
import pickle
import argparse
from datetime import datetime
from io import BytesIO, StringIO

# Related third party imports
import cv2
import socketio
import eventlet
import eventlet.wsgi
import numpy as np
import matplotlib.image as mpimg
from PIL import Image
from flask import Flask

# Local application/library specific imports
from perception import perception_step
import decision_new
from supporting_functions import update_rover, create_output_images

# Initialize socketio server and Flask application
sio = socketio.Server()
app = Flask(__name__)

# Read in ground truth map and create 3-channel green version for overplotting
# NOTE: images are read in by default with the origin (0, 0) in the upper left
# and y-axis increasing downward.
ground_truth = mpimg.imread('../calibration_images/map_bw.png')

# This next line creates arrays of zeros in the red and blue channels
# and puts the map into the green channel.  This is why the underlying
# map output looks green in the display image
ground_truth_3d = np.dstack(
    (ground_truth*0, ground_truth*255, ground_truth*0)
).astype(np.float)


class RoverTelemetry():
    """
    Create a class to be a container for rover state telemetry values.

    This allows for tracking telemetry values and results from
    perception analysis

    """

    def __init__(self):
        """
        Initialize a RoverTelemetry instance to retain parameters.

        NOTE: distances in meters and angles in degrees

        """
        self.start_time = None  # To record the start time of navigation
        self.total_time = None  # To record total duration of navigation
        self.img = None  # Current camera image
        self.pos = None  # Current position (x, y)
        self.yaw = None  # Current yaw angle
        self.pitch = None  # Current pitch angle
        self.roll = None  # Current roll angle
        self.vel = None  # Current velocity (m/s)
        self.steer = 0  # Current steering angle
        self.throttle = 0  # Current throttle value
        self.brake = 0  # Current brake value

        self.nav_dists = None  # Distances to navigable terrain pixels
        self.nav_angles = None  # Angles of navigable terrain pixels
        self.nav_angles_left = None  # Nav terrain angles left of rover heading

        self.obs_dists = None  # Distances to obstacle terrain pixels
        self.obs_angles = None  # Angles of obstacle terrain pixels

        self.rock_dists = None  # Distances to rock terrain pixels
        self.rock_angles = None  # Angles of rock terrain pixels

        self.samples_pos = None  # To store the actual sample positions
        self.samples_to_find = 0  # To store the initial count of samples
        self.samples_collected = 0  # To count the number of samples collected
        self.near_sample = 0  # To be set to TLM value data["near_sample"]
        self.picking_up = 0  # To be set to TLM value data["picking_up"]
        self.send_pickup = False  # Set to True to trigger rock pickup

        self.home_distance = None  # Current distance to starting location
        self.home_heading = None  # Current heading to starting location
        self.going_home = False  # Default rover configuration

        self.timer_on = False  # Timer to determine duration of stuck
        self.stuck_heading = 0.0  # Heading at the time of getting stuck

        # Rover vision image to be updated with displays of
        # intermediate analysis steps on screen in autonomous mode
        self.vision_image = np.zeros((160, 320, 3), dtype=np.float)

        # Worldmap image to be updated with the positions of
        # ROIs navigable terrain, obstacles and rock samples
        self.worldmap = np.zeros((200, 200, 3), dtype=np.float)
        self.ground_truth = ground_truth_3d  # Ground truth worldmap
        # To update % of ground truth map successfully found
        self.perc_mapped = 0


# Initialize our rover
Rover = RoverTelemetry()

# Initialize decision supervisor
Decider = decision_new.DecisionSupervisor()

# Variables to track frames per second (FPS)
# Initialize frame counter
frame_counter = 0

# Initialize second counter
second_counter = time.time()
fps = None


# Define telemetry function for what to do with incoming data
@sio.on('telemetry')
def telemetry(sid, data):
    """
    Handle incoming telemetry data.

    Run every time the simulator sends a new batch of data
    (nominally 25 times per second)

    """
    global frame_counter, second_counter, fps
    frame_counter += 1
    # Do a rough calculation of frames per second (FPS)
    if (time.time() - second_counter) > 1:
        fps = frame_counter
        frame_counter = 0
        second_counter = time.time()
    print("Current FPS: {}".format(fps))

    if data:
        global Rover
        # Initialize / update Rover with current telemetry
        Rover, image = update_rover(Rover, data)

        if np.isfinite(Rover.vel):

            # Execute perception and decision steps to update Rover's telemetry
            Rover = perception_step(Rover)
            Rover = Decider.execute(Rover)

            # Create output images to send to server
            out_image_strings = create_output_images(Rover, Decider)
            out_image_string1, out_image_string2 = out_image_strings

            # The action step!  Send commands to the rover!

            # Don't send both of these, they both trigger the simulator
            # to send back new telemetry so we must only send one
            # back in response to the current telemetry data.

            # If in a state where want to pickup a rock send pickup command
            if Rover.send_pickup and not Rover.picking_up:
                send_pickup()
                Rover.send_pickup = False  # Reset Rover flags
            else:
                # Send commands to the rover!
                commands = (Rover.throttle, Rover.brake, Rover.steer)
                send_control(commands, out_image_string1, out_image_string2)

        # In case of invalid telemetry, send null commands
        else:

            # Send zeros for throttle, brake and steer and empty images
            send_control((0, 0, 0), '', '')

        # To save camera images from autonomous driving, specify a path
        # Example: $ python drive_rover.py image_folder_path
        # Conditional to save image frame if folder was specified
        if args.image_folder != '':
            timestamp = datetime.utcnow().strftime('%Y_%m_%d_%H_%M_%S_%f')[:-3]
            image_filename = os.path.join(args.image_folder, timestamp)
            image.save('{}.jpg'.format(image_filename))

    else:
        sio.emit('manual', data={}, skip_sid=True)


@sio.on('connect')
def connect(sid, environ):
    """Invoke the connect event handler."""
    print("connect ", sid)
    send_control((0, 0, 0), '', '')
    sample_data = {}
    sio.emit(
        "get_samples",
        sample_data,
        skip_sid=True)


def send_control(commands, image_string1, image_string2):
    """Send control commands to the rover."""
    data = {
        'throttle': commands[0].__str__(),
        'brake': commands[1].__str__(),
        'steering_angle': commands[2].__str__(),
        'inset_image1': image_string1,
        'inset_image2': image_string2,
        }
    # Send commands via socketIO server
    sio.emit(
        "data",
        data,
        skip_sid=True)
    eventlet.sleep(0)


def send_pickup():
    """Send command to pickup rock sample."""
    print("Picking up")
    pickup = {}
    sio.emit(
        "pickup",
        pickup,
        skip_sid=True)
    eventlet.sleep(0)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Remote Driving')
    parser.add_argument(
        'image_folder',
        type=str,
        nargs='?',
        default='',
        help='Path to image folder.' +
        ' This is where the images from the run will be saved.'
    )
    args = parser.parse_args()

    #os.system('rm -rf IMG_stream/*')
    if args.image_folder != '':
        print("Creating image folder at {}".format(args.image_folder))
        if not os.path.exists(args.image_folder):
            os.makedirs(args.image_folder)
        else:
            shutil.rmtree(args.image_folder)
            os.makedirs(args.image_folder)
        print("Recording this run ...")
    else:
        print("NOT recording this run ...")

    # wrap Flask application with socketio's middleware
    app = socketio.Middleware(sio, app)

    # deploy as an eventlet WSGI server
    eventlet.wsgi.server(eventlet.listen(('', 4567)), app)
