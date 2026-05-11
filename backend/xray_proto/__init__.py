"""Optional placeholder package for generated Xray protobuf stubs.

The runtime HandlerService client in backend/xray_handler_api.py does NOT
depend on this package — it hand-encodes the small set of protobuf messages
it needs (same style as backend/xray_stats.py).

This directory is the destination for the optional code-generation script
deploy/generate_xray_protos.sh. Stubs are not committed by default; the
package marker file is kept so the directory can be imported as a Python
package when stubs are generated for local development.
"""
