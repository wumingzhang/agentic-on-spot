"""Make `import spot_handoff` work from the tests, whichever way they are run.

`python3 -m unittest discover -s tests` imports test modules top-level (not
as the `tests` package), so this is imported explicitly by support.py and by
tests/__init__.py rather than relying on package initialisation. Bytecode
writing is turned off so a test run never leaves __pycache__ inside the Lambda
source tree that Terraform zips.
"""

import os
import sys

sys.dont_write_bytecode = True
LAMBDA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lambda")
if LAMBDA_DIR not in sys.path:
    sys.path.insert(0, LAMBDA_DIR)
