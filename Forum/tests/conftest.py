import os
import tempfile

# Forum's config needs Razorpay test creds present at import time.
os.environ.setdefault("RAZORPAY_KEY_ID", "rzp_test_dummy")
os.environ.setdefault("RAZORPAY_KEY_SECRET", "dummy_secret")
os.environ.setdefault("RAZORPAY_MODE", "test")
os.environ.setdefault("FORUM_LEDGER_DB", os.path.join(tempfile.gettempdir(), "forum_test_ledger.db"))
