"""EC2 Spot interruption -> SSM Run Command -> docker exec notification.

The package has no AWS dependency at import time: boto3 is imported inside
handler.make_clients so the test-suite runs with the standard library alone.
"""

__all__ = ["config", "validation", "render", "handler"]
