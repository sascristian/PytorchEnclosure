import requests
import os
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("workflow_run_id", help="The id of the workflow run")
parser.add_argument("job_name", help="The name of the job to retrieve the id for")

args = parser.parse_args()


PYTORCH_REPO = "https://api.github.com/repos/pytorch/pytorch"
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
REQUEST_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "Authorization": "token " + GITHUB_TOKEN,
}

response = requests.get(
    # No f-strings because our CI needs to be able to run on older Python versions
    PYTORCH_REPO + "/actions/runs/" + args.workflow_run_id + "/jobs?per_page=100",
    headers=REQUEST_HEADERS,
)
json = response.json()
jobs = json["jobs"]

for job in jobs:
    if job["name"] == args.job_name:
        print(job["id"])
        exit(0)

exit(1)
