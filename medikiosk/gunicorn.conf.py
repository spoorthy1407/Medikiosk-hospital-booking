"""gunicorn settings for a server:  gunicorn app:app -c gunicorn.conf.py"""
import os

bind = "0.0.0.0:" + os.environ.get("PORT", "8000")
workers = 1        # one process: the background jobs (Excel refresh, reminder e-mails) must run only once
threads = 8        # many visitors at the same time
timeout = 90
accesslog = "-"


def post_worker_init(worker):
    import app
    app.start_background_jobs()
