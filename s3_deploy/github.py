
#!/usr/bin/env python
"""
Comments stdin to the GitHub PR that triggered the travis build.
Usage:
    flake8 | python comment-on-pr.py
Notes:
    The following enviromental variables need to be set:
    - TRAVIS_PULL_REQUEST
    - TRAVIS_REPO_SLUG
    - TRAVIS_BOT_GITHUB_TOKEN
"""
import json
import requests

GITHUB_API_URL = 'https://api.github.com'


def comment_on_pull_request(pr_number, slug, token, comment):
    """ Comment message on a given GitHub pull request. """
    url = '{api_url}/repos/{slug}/issues/{number}/comments'.format(
        api_url=GITHUB_API_URL, slug=slug, number=pr_number)
    print(url)
    response = requests.post(url, data=json.dumps({'body': comment}),
                             headers={'Authorization': 'token ' + token})
    print(response)
    return response.json()


def build_comment(branch_name):
    site_name = 'bw-docs-' + branch_name
    comment = """Preview Changes at:
        http://%s.s3-website-us-east-1.amazonaws.com/""" % site_name
    return comment