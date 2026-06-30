# KiteCLI - Kite Connect CLI

import socket
import urllib3.util.connection as connection

# Force IPv4 resolution globally to prevent macOS IPv6 lookup timeouts/delays
# and resolve "IP not allowed" errors where Zerodha registers static IPv4 addresses.
def allowed_gai_family():
    return socket.AF_INET

connection.allowed_gai_family = allowed_gai_family
