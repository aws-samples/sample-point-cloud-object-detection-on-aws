"""Settings every test starts from.

The AWS-mode API refuses to start without ACCESS_SCOPE (api/identity.py). The suite
runs in shared mode, the API's behaviour before per-user access; the per-user tests
switch it themselves (tests/_identity.py). Set here, at import, rather than in a
fixture, because some CI jobs run tests without the project's dependencies and
nothing here may import it.
"""

import os

os.environ["ACCESS_SCOPE"] = "shared"
for _name in ("ALB_ARN", "COGNITO_USER_POOL_ID", "COGNITO_CLIENT_ID"):
    os.environ.pop(_name, None)
