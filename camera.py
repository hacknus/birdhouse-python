import time
import RPi.GPIO as GPIO

from ignore_motion import ignore_motion_for

# Toggle IR LED (ON or OFF)

# Setup GPIO mode
GPIO.setmode(GPIO.BCM)

# Set the GPIO pin that controls the IR LED
IR_LED_PIN = 17  # Change this pin number to match your setup
GPIO.setup(IR_LED_PIN, GPIO.OUT)
ignore_motion_for(10)
GPIO.output(IR_LED_PIN, GPIO.LOW)

ir_led_state = False

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
