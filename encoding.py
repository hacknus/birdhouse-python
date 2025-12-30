import base64
from dotenv import dotenv_values

env_values = dotenv_values("mainapp/bird.env")

custom_key = env_values['ENCODING']


def xor_encrypt_decrypt(data, key):
    """XOR-based encryption and decryption (symmetric)."""
    key_length = len(key)
    return ''.join(chr(ord(data[i]) ^ ord(key[i % key_length])) for i in range(len(data)))


def encode_email(email):
    """Encodes (hashes) the email."""
    xor_result = xor_encrypt_decrypt(email, custom_key)
    encoded = base64.urlsafe_b64encode(xor_result.encode()).decode()
    return encoded


def decode_email(encoded_email):
    """Decodes (unhashes) the email."""
    decoded_bytes = base64.urlsafe_b64decode(encoded_email)
    original_email = xor_encrypt_decrypt(decoded_bytes.decode(), custom_key)
    return original_email
