import base64
import time

from Crypto.Cipher import AES
from Crypto import Random
from dotenv import dotenv_values

BS = AES.block_size
pad = lambda s: s + (BS - len(s) % BS) * chr(BS - len(s) % BS)
unpad = lambda s: s[:-ord(s[len(s) - 1:])]


class Cipher:

    def __init__(self, key_file, nonce_expiration_seconds=30):
        env_values = dotenv_values(key_file)
        self.key = env_values["TCP_ENCRYPTION_KEY"]
        self.nonce_set = set()
        self.nonce_expiration_seconds = nonce_expiration_seconds

    def encrypt_message(self, message):
        message = pad(message)
        iv = Random.new().read(AES.block_size - 8)
        timestamp = int(time.time()).to_bytes(8, byteorder="little")
        enc = AES.new(self.key.encode("utf8"), AES.MODE_CBC, timestamp + iv)
        return base64.b64encode(timestamp + iv + enc.encrypt(message.encode("utf8")))

    def cleanup_expired_nonces(self):
        # Remove expired nonces from the set
        expired_nonces = {(timestamp, iv) for (timestamp, iv) in self.nonce_set if
                          int.from_bytes(timestamp, byteorder="little") < time.time() - self.nonce_expiration_seconds}
        self.nonce_set -= expired_nonces

    def is_nonce_used(self, timestamp_nonce):
        # Check if the nonce has been used before
        self.cleanup_expired_nonces()  # Ensure cleanup before checking
        return timestamp_nonce in self.nonce_set

    def decrypt_message(self, ciphertext):
        ciphertext = base64.b64decode(ciphertext)
        timestamp = ciphertext[:8]
        iv = ciphertext[8:AES.block_size]
        timestamp_int = int.from_bytes(timestamp, byteorder="little")
        if (timestamp_int < time.time() - self.nonce_expiration_seconds or
                timestamp_int > time.time() + self.nonce_expiration_seconds):
            raise Exception("Expired timestamp")
        if self.is_nonce_used((timestamp, iv)):
            raise Exception("Replay Attack detected")
        self.nonce_set.add((timestamp, iv))
        message = ciphertext[AES.block_size:]
        dec = AES.new(self.key.encode("utf8"), AES.MODE_CBC, timestamp + iv)
        decrypted_message = unpad(dec.decrypt(message)).decode("utf-8")
        return decrypted_message


if __name__ == '__main__':
    cipher = Cipher("../coconut.env")
    message = "[CMD] setTemperature=30.0\r\n"
    ciphertext = cipher.encrypt_message(message)
    print(f"command sent the first time: {ciphertext}")
    message = cipher.decrypt_message(ciphertext)
    print(f"command decrypted: {message}")
    ciphertext = cipher.encrypt_message(message)
    print(f"command sent the second time: {ciphertext}")
    message = cipher.decrypt_message(ciphertext)
    print(f"command decrypted: {message}")
