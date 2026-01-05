import queue
import socket
import socketserver
import threading
import time
import logging
from pathlib import Path

from encryption import Cipher


class ThreadedTCPRequestHandler(socketserver.BaseRequestHandler):
    timeout = 10
    connected_clients = set()

    def receive(self):
        raw = str(self.request.recv(1024), 'ascii')
        if raw and hasattr(self.server, 'cipher'):
            # message needs to be terminated by newline character
            if raw[-1] == "\n":
                raw.replace("\n", "")
                return self.server.cipher.decrypt_message(raw)

    def send(self, raw):
        if raw and hasattr(self.server, 'cipher'):
            encrypted = self.server.cipher.encrypt_message(raw)
            self.request.sendall(encrypted)

    def setup(self):
        self.connection = self.request
        if self.timeout is not None:
            self.connection.settimeout(self.timeout)
        # waiting for authentication
        # the client needs to send its encrypted ip address, we then decrypt the message and
        # check it against the connected ip address, if this is a match we are authenticated
        try:
            ip_address = self.receive()
            if ip_address == self.client_address[0]:
                if hasattr(self.server, 'full_encryption') and self.server.full_encryption:
                    self.send("[ACK] authentication successful\n")
                else:
                    self.request.sendall(b"[ACK] authentication successful\n")
                self.timeout = None
                self.connection.settimeout(self.timeout)
                self.connected_clients.add(self.request)
                logging.info(f"[TCP] Successful login attempt from {self.client_address[0]}")
            else:
                # this we send unencrypted!
                self.request.sendall(b"[ERR] invalid token\n")
                self.request.close()
                logging.info(
                    f"[TCP] Failed login attempt from {self.client_address[0]} with encrypted ip_address: {ip_address}")
        except socket.timeout:
            # this we send unencrypted!
            self.request.sendall(b"[ERR] timeout for login attempt\n")
            self.request.close()
            logging.info(f"[TCP] Timeout for login attempt from {self.client_address[0]}")
            pass
        except BrokenPipeError:
            logging.info(f"[TCP] Connection closed from {self.client_address[0]}")

    def handle(self):
        try:
            if self.request not in self.connected_clients:
                return
            while True:
                data = self.receive()
                if not data:
                    # Connection closed by the client
                    logging.info(f"[TCP] Connection closed by {self.client_address}\n")
                    break

                # Handle the received data here
                print(f"[TCP] Received data from {self.client_address}: {data}")

                # You can send a response back to the client if needed
                # self.request.sendall(b"Response from server")

                if (hasattr(self.server, 'run_tasks') and hasattr(self.server, 'tcp_cmd_queue')
                        and hasattr(self.server, 'tcp_cmd_ack_queue')):
                    self.server.tcp_cmd_queue.put(data)

                    # note: we cannot use queue.get(timeout=timeout) because this would let this task stuck even when the 
                    # main task is killed
                    start = time.time()
                    response = None
                    while self.server.run_tasks:
                        if time.time() - start > 10.0:
                            break
                        try:
                            response = self.server.tcp_cmd_ack_queue.get(block=False)
                            break
                        except queue.Empty:
                            time.sleep(0.25)
                            continue
                    if response:
                        if hasattr(self.server, 'full_encryption') and self.server.full_encryption:
                            self.send(response)
                        else:
                            self.request.sendall(response.encode('utf-8'))
                    else:
                        if hasattr(self.server, 'full_encryption') and self.server.full_encryption:
                            self.send("[ERR] command not acknowledged\n")
                        else:
                            self.request.sendall("[ERR] command not acknowledged\n".encode('utf-8'))
                else:
                    raise AttributeError(
                        "Server does not have a tcp_cmd_queue, tcp_cmd_ack_queue, run_tasks, authenticated_clients")

        except ConnectionResetError:
            # Connection reset by the client
            logging.info(f"[TCP] Connection reset by {self.client_address}")
        except TimeoutError:
            logging.info(f"[TCP] Timeout from {self.client_address}")
        except BrokenPipeError:
            logging.info(f"[TCP] Connection closed from {self.client_address[0]}")
        except KeyboardInterrupt:
            self.server.shutdown()
        finally:
            if self.request in self.connected_clients:
                self.connected_clients.remove(self.request)
            self.request.close()


class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    pass


class CommandServer(ThreadedTCPServer):
    def __init__(self, tcp_cmd_queue: queue.Queue, tcp_cmd_ack_queue: queue.Queue, env_file: Path, run_tasks: bool,
                 full_encryption: bool, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.tcp_cmd_queue = tcp_cmd_queue
        self.tcp_cmd_ack_queue = tcp_cmd_ack_queue
        self.run_tasks = run_tasks
        self.cipher = Cipher(env_file)
        self.full_encryption = full_encryption


def send_periodic_data_to_all_clients(tcp_rep_queue: queue.Queue, env_file: Path, full_encryption: bool = False):
    cipher = Cipher(env_file)
    while True:
        # Send data to all connected clients periodically

        # note: we cannot use queue.get(timeout=timeout) because this would let this task stuck even when the 
        # main task is killed
        message = None
        while True:
            try:
                message = tcp_rep_queue.get(block=False)
                break
            except queue.Empty:
                time.sleep(0.25)
                continue
        try:
            for client in ThreadedTCPRequestHandler.connected_clients:
                # note that full encryption takes more time and is not necessary since the data itself is
                # not private
                if full_encryption:
                    encrypted_message = cipher.encrypt_message(message)
                    encrypted_message += b"\n"
                    client.sendall(encrypted_message)
                else:
                    client.sendall(message.encode('utf-8'))
        except Exception as e:
            logging.error(f"[TCP] Error sending data to client: {e}")


def run_server(tcp_cmd_queue: queue.Queue, tcp_cmd_ack_queue: queue.Queue, tcp_rep_queue: queue.Queue, env_file: Path,
               run_tasks: bool, full_encryption: bool = False, ip='0.0.0.0', port=65432):
    server = CommandServer(tcp_cmd_queue, tcp_cmd_ack_queue, env_file, run_tasks, full_encryption, (ip, port),
                           ThreadedTCPRequestHandler)
    ip, port = server.server_address
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()

    # Start a separate thread for sending periodic data to all clients
    periodic_thread = threading.Thread(target=send_periodic_data_to_all_clients,
                                       args=(tcp_rep_queue, env_file, full_encryption))
    periodic_thread.daemon = True
    periodic_thread.start()

    logging.info(f"[TPC] Server loop running on: {ip}, {port}")
    return server


if __name__ == "__main__":
    logging.basicConfig(filename=f"../log/log_{time.time()}.log",
                        filemode='a',
                        format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                        datefmt='%H:%M:%S',
                        level=logging.DEBUG)

    run_tasks = True
    tcp_cmd_queue = queue.Queue()
    tcp_cmd_ack_queue = queue.Queue()
    tcp_rep_queue = queue.Queue()
    server = run_server(tcp_cmd_queue, tcp_cmd_ack_queue, tcp_rep_queue, Path("../coconut.env"), run_tasks,
                        False, port=65432, ip='0.0.0.0')
    tcp_cmd_ack_queue.put("Pong")
    time.sleep(2)

    for i in range(1000):
        print(i)
        tcp_rep_queue.put(f"[REP] pos = {i}\r\n")

    # run the server for 60 seconds
    time.sleep(60)
