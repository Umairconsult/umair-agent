"""Runs first in every GitHub run. Makes sure every file of the agent was uploaded."""
import os, sys

REQUIRED = [
    "requirements.txt",
    "agent/__init__.py", "agent/__main__.py", "agent/agent_utils.py", "agent/audit.py", "agent/blog.py", "agent/cities.py",
    "agent/config.py", "agent/followups.py", "agent/logutil.py", "agent/niches.py", "agent/osm.py", "agent/overture.py",
    "agent/portal.py", "agent/scheduler.py", "agent/seo.py", "agent/slack.py", "agent/web.py", "agent/wordpress.py", "agent/writer.py",
]
missing = [f for f in REQUIRED if not os.path.isfile(f)]
if missing:
    print("PROBLEM: these files are missing from your GitHub repository:")
    for f in missing:
        print("   -", f)
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print("::error title=Files missing on GitHub::Missing: " + ", ".join(missing) + ". Unzip umair-agent.zip and upload the agent folder again (Add file > Upload files).")
    print("Fix: unzip umair-agent.zip on your computer, then in GitHub click Add file > Upload files and drag in the agent folder "
          "(and requirements.txt) again. Missing files inside a folder mean the upload skipped them - upload that folder again.")
    sys.exit(1)
print("All", len(REQUIRED), "agent files are present.")
