import time
ignore_motion_until = 0  # Timestamp until which motion detection is ignored


def ignore_motion_for(seconds):
    """Temporarily disable motion detection for a given number of seconds."""
    global ignore_motion_until
    ignore_motion_until = time.time() + seconds

def are_we_still_blocked():
    if time.time() < ignore_motion_until:
        return True
    else:
        return False
