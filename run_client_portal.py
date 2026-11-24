"""Start the CLIENT PORTAL - the clients' own dashboard, on its own port.

    python run_client_portal.py                # this PC only: http://127.0.0.1:8090
    python run_client_portal.py --port 8091

It is a separate web server from the desk: it can read a client's numbers
and nothing else - it cannot trade. Clients sign in with the username and
password set on the Managed Accounts screen.

OPENING IT TO CLIENTS is a deliberate step: `--host 0.0.0.0` makes it
answer from other machines, and on the internet it must sit behind HTTPS
(a reverse proxy such as Caddy or nginx, or a tunnel such as Cloudflare
Tunnel) - never plain http, which would send passwords in the clear.
The desk itself should never be opened this way.
"""

import argparse
import os

from okxtrader.client_portal import create_portal


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--host', default='127.0.0.1',
                        help='127.0.0.1 = this PC only (the default)')
    parser.add_argument('--port', type=int, default=8090)
    parser.add_argument('--config', default='config.json')
    parser.add_argument('--status', default='status.json')
    args = parser.parse_args()
    if args.host not in ('127.0.0.1', 'localhost'):
        print('WARNING: the client portal is answering other machines. '
              'Put it behind HTTPS before any client uses it.')
    app = create_portal(os.path.abspath(args.config),
                        os.path.abspath(args.status))
    print(f'Client portal on http://{args.host}:{args.port} '
          f'(it reads the desk’s records; it cannot trade)')
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == '__main__':
    main()
