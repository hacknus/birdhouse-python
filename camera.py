import time
import RPi.GPIO as GPIO

from ignore_motion import ignore_motion_for

# Toggle IR LED (ON or OFF)

# Setup GPIO mode
GPIO.setmode(GPIO.BCM)

# Set the GPIO pin that controls the IR LED
IR_LED_PIN = 17  # Change this pin number to match your setup
IR_FILTER_A_PIN = 20
IR_FILTER_B_PIN = 21
GPIO.setup(IR_LED_PIN, GPIO.OUT)
GPIO.setup(IR_FILTER_A_PIN, GPIO.OUT)
GPIO.setup(IR_FILTER_B_PIN, GPIO.OUT)
ignore_motion_for(10)
GPIO.output(IR_LED_PIN, GPIO.LOW)
GPIO.output(IR_FILTER_A_PIN, GPIO.LOW)
GPIO.output(IR_FILTER_B_PIN, GPIO.LOW)

ir_led_state = False
ir_filter_state = False


def turn_ir_on():
    global ir_led_state
    ir_led_state = True
    GPIO.output(IR_LED_PIN, GPIO.HIGH)


def turn_ir_off():
    ignore_motion_for(10)
    global ir_led_state
    ir_led_state = False
    GPIO.output(IR_LED_PIN, GPIO.LOW)


def get_ir_led_state():
    global ir_led_state
    time.sleep(0.1)
    return ir_led_state


def turn_ir_filter_on():
    global ir_filter_state
    ir_filter_state = True
    GPIO.output(IR_FILTER_A_PIN, GPIO.HIGH)
    GPIO.output(IR_FILTER_B_PIN, GPIO.LOW)
    time.sleep(1.0)
    GPIO.output(IR_FILTER_A_PIN, GPIO.LOW)
    GPIO.output(IR_FILTER_B_PIN, GPIO.LOW)


def turn_ir_filter_off():
    ignore_motion_for(10)
    global ir_filter_state
    ir_filter_state = False
    GPIO.output(IR_FILTER_A_PIN, GPIO.LOW)
    GPIO.output(IR_FILTER_B_PIN, GPIO.HIGH)
    time.sleep(1.0)
    GPIO.output(IR_FILTER_A_PIN, GPIO.LOW)
    GPIO.output(IR_FILTER_B_PIN, GPIO.LOW)


def get_ir_filter_state():
    global ir_filter_state
    time.sleep(0.1)
    return ir_filter_state
