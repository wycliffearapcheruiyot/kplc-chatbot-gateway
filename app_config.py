"""
app_config.py

The gateway's single RuntimeConfig. Every module reads settings through `cfg`
(override from the admin panel -> real env var -> default); see runtime_config.py.
main.py binds it to the MongoDB database once that exists.
"""

from runtime_config import make
from settings_catalog import names_for

cfg = make("gateway", names_for("gateway"))
