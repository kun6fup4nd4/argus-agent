import os
import warnings
# Suppress urllib3 SSL warning
warnings.filterwarnings('ignore', message='urllib3 v2 only supports OpenSSL')

import requests
import json
import base64
import sys
import time
import re
import subprocess
from urllib.parse import urlparse, unquote

from agno.agent import Agent
from agno.models.google import Gemini
from agno.models.openai import OpenAIChat

import argparse
from typing import List

from rich.console import Console
from rich.markdown import Markdown

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import traceback

LINEAR_API_KEY = os.getenv("LINEAR_API_KEY")
GITHUB_TOKEN = os.getenv("GITHUB_ACCESS_TOKEN")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
TIMESTAMP_FILE = "last_slack_timestamp.txt" # file to save timestamp of last message checked in slack

def parse_pull_requests(ticket_string):
    # Check if ticket_string is empty or None
    if not ticket_string:
        return []
    
    # Check if the expected part exists in the string
    if "PULL_REQUESTS_TO_REVIEW: " not in ticket_string:
        return []
    
    tickets_part = ticket_string.split("PULL_REQUESTS_TO_REVIEW: ")[1]
    
    # Handle empty list case
    if not tickets_part.strip():
        return []
        
    ticket_list = [ticket.strip() for ticket in tickets_part.split(",")]
    return ticket_list

def _find_channel_id(client: WebClient, channel_name: str, channel_types: str) -> str | None:
    """Helper function to find a channel ID, handling pagination."""
    channel_id = None
    cursor = None
    while True: # Loop until break (found or no more pages)
        try:
            print(f"Searching for '{channel_name}' in {channel_types} (cursor: {cursor})...")
            result = client.conversations_list(
                types=channel_types,
                exclude_archived=(channel_types == "public_channel"), # Only exclude for public
                limit=200,  # Fetch more channels per API call
                cursor=cursor
            )

            if not result.get("ok"):
                print(f"Error listing {channel_types}: {result.get('error', 'Unknown error')}")
                return None # Stop searching this type on error

            channels = result.get("channels", [])
            print(f"  Found {len(channels)} channels in this batch.")
            for channel in channels:
                # print(f"    Checking channel: {channel.get('name', 'N/A')}") # Uncomment for detailed debugging
                if channel.get("name") == channel_name:
                    print(f"  Found matching channel: ID {channel['id']}, Name: {channel['name']}")
                    return channel["id"] # Found it!

            # Check for next page
            cursor = result.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                print(f"  No more pages for {channel_types}.")
                break # No more pages

            print(f"  Moving to next page (new cursor: {cursor})...")
            # Optional: Add a small delay to respect rate limits if you have many channels
            # time.sleep(1) 

        except SlackApiError as e:
            # Specific handling for rate limits
            if e.response.status_code == 429:
                retry_after = int(e.response.headers.get('Retry-After', 1))
                print(f"Rate limited. Retrying after {retry_after} seconds...")
                time.sleep(retry_after)
                # Continue the loop to retry the same request (cursor remains the same)
                continue 
            else:
                print(f"Slack API Error listing {channel_types}: {e.response['error']}")
                return None # Stop searching this type on other errors
        except Exception as e:
             print(f"An unexpected error occurred while listing {channel_types}: {e}")
             return None

    print(f"Channel '{channel_name}' not found in {channel_types}.")
    return None # Channel not found after checking all pages

def check_new_slack_messages(client: WebClient, channel_name: str, oldest_timestamp: str = None) -> tuple[list, str | None]:
    new_messages = []
    latest_ts_in_batch = oldest_timestamp 

    try:
        channel_id = None

        print(f"Attempting to find public channel '{channel_name}'...")
        channel_id = _find_channel_id(client, channel_name, "public_channel")
        
        # If not found, try private channels (if you have permissions)
        if not channel_id:
            print(f"\nAttempting to find private channel '{channel_name}'...")
            # Note: Requires 'groups:read' scope for the token
            channel_id = _find_channel_id(client, channel_name, "private_channel")
        
        if not channel_id:
            print(f"Channel '{channel_name}' not found.")
            return [], oldest_timestamp

        result = client.conversations_history(
            channel=channel_id,
            oldest=oldest_timestamp,
            limit=200  
        )

        if not result["ok"]:
            print(f"Error fetching messages: {result.get('error', 'Unknown error')}")
            return [], oldest_timestamp 

        messages = result.get("messages", [])
        if messages:
            new_messages = messages
            latest_ts_in_batch = messages[0]['ts']
        else:
            print("No new messages found.")
        return new_messages, latest_ts_in_batch

    except SlackApiError as e:
        print(f"Slack API Error occurred: {e.response['error']}")
        return [], oldest_timestamp 
    except Exception as e:
        print(f"An unexpected error occurred: {e}")
        return [], oldest_timestamp

def extract_github_links(text: str) -> List[str]:
    # Regular expression to match GitHub links
    # This pattern matches standard GitHub URLs with or without www prefix
    github_pattern = r'https?://(?:www\.)?github\.com/[a-zA-Z0-9-]+/[a-zA-Z0-9._-]+(?:/(?:pull|issues)/\d+)?'
    
    # Find all matches
    all_links = re.findall(github_pattern, text)
    
    # Remove any trailing characters that shouldn't be part of the URL
    cleaned_links = []
    for link in all_links:
        # Remove trailing punctuation that might have been included
        if link.endswith(')') and '(' not in link:
            link = link[:-1]
        if link.endswith(']') and '[' not in link:
            link = link[:-1]
        if any(link.endswith(c) for c in ',.;:\'"`'):
            link = link[:-1]
            
        cleaned_links.append(link)
    
    # Convert to a set to remove duplicates, then back to a list
    unique_links = list(set(cleaned_links))
    
    # Sort links for consistent output (PR links appear after repository links)
    return sorted(unique_links, key=lambda x: ('pull' not in x and 'issues' not in x, x))

def fetch_file_from_github_api(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    path_parts = parsed.path.strip("/").split("/")

    try:
        owner = path_parts[0]
        repo = path_parts[1]
        commit = path_parts[3]
        filepath = "/".join(path_parts[4:])
        filepath = unquote(filepath)

        api_url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filepath}?ref={commit}"
        headers = {
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        response = requests.get(api_url, headers=headers)
        if response.status_code == 200:
            data = response.json()
            if data.get("encoding") == "base64":
                return base64.b64decode(data["content"]).decode("utf-8")
            else:
                return data["content"]
        else:
            print(f"Failed to fetch {filepath} from GitHub API: {response.status_code}")
            return ""
    except Exception as e:
        print(f"Error while parsing URL or fetching file: {e}")
        return ""

def get_linear_issues(specific_ticket):
    """
    Fetches linear ticket should not by in specific ignored states like "Done", "Canceled", etc.
    
    Args:
        specific_ticket (str or list, optional): A specific ticket identifier (e.g., "TEAM1-2146")
            or a list of ticket identifiers (e.g., ["TEAM1-2146", "TEAM1-2079"])
            
    Returns:
        dict: The parsed JSON response from Linear's GraphQL API.
    """

    query = """
    query IssuesByLabel($filter: IssueFilter) {
        issues(filter: $filter, first: 250) {
            nodes {
                id
                title
            }
        }
    }
    """
    
    # Default states to exclude
    excluded_states = ["Done", "Canceled", "Duplicate", "Backlog", "Todo", "In Progress", "Triage"]        
    variables = {
        "filter": {
            "state": {
                "name": {
                    "nin": excluded_states
                }
            }
        }
    }
    
    # Convert single ticket to list for uniform processing
    ticket_list = [specific_ticket] if isinstance(specific_ticket, str) else specific_ticket   
    
    # Create a list to hold OR conditions for each ticket
    or_conditions = []
    
    for ticket in ticket_list:
        # Extract the project identifier and number
        if "-" in ticket:
            project, number = ticket.split("-", 1)
            
            # Create a condition for this specific ticket
            ticket_condition = {
                "and": [
                    {"team": {"key": {"eq": project}}},
                    {"number": {"eq": int(number)}}
                ]
            }            
            or_conditions.append(ticket_condition)
    
    # If we have valid conditions, replace the filter with an OR filter
    if or_conditions:
        # Keep the label filter but replace team/number with OR conditions
        state_filter = variables["filter"]["state"]
        
        # Use an OR condition that includes all specified tickets
        variables["filter"] = {
            "or": or_conditions,
            "state": state_filter
        }
        
    
    response = requests.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables},
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json"
        }
    )    
    return json.dumps(response.json())


def get_linear_issue_details(issue_id: str) -> dict:
    """
    Fetches detailed information for a specific Linear issue.

    Args:
        issue_id (str): The unique identifier of the Linear issue.

    Returns:
        dict: A dictionary containing issue details (title, description, labels, comments, attachments, etc.)
              or None if the issue was not found.
    """
    query = """
    query GetIssueDetails($id: String!) {
        issue(id: $id) {
            id
            title
            description
            priority
            state {
                name
            }
            assignee {
                name
                email
            }
            labels {
                nodes {
                    name
                }
            }
            comments {
                nodes {
                    body
                    createdAt
                    user {
                        name
                    }
                }
            }
            attachments {
                nodes {
                    title
                    url
                    metadata
                    sourceType
                }
            }
        }
    }
    """

    variables = {"id": issue_id}

    response = requests.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables},
        headers={
            "Authorization": LINEAR_API_KEY,
            "Content-Type": "application/json"
        }
    )

    data = response.json()
    return json.dumps(data.get("data", {}).get("issue", None))

def extract_repo_name(git_url):
    """
    Extract the repository name (second to last element) from a git URL.
    This is the default directory name created when cloning a repository.
    
    Args:
        git_url (str): The git URL, like 'git://github.com/<complany>/<repo>.git'
        
    Returns:
        str: The repository name (e.g., 'ethereum')
    """
    # Remove trailing .git if present
    if git_url.endswith('.git'):
        git_url = git_url[:-4]
    
    # Split the URL by '/'
    parts = git_url.split('/')
    
    # Return the second to last element (repository name)
    if len(parts) >= 2:
        return parts[-1]
    else:
        return None

def is_test_file(filename: str) -> bool:
    """
    Determines if a file is a test file based on its name or path.
    """
    # Convert to lowercase for case-insensitive matching
    filename_lower = filename.lower()
    
    # Check for test directories
    if filename_lower.startswith('test/') or filename_lower.startswith('tests/'):
        return True
        
    # Check for test file extensions
    if '.test.' in filename_lower or '.spec.' in filename_lower:
        return True
    
    # Check other patterns
    test_patterns = [
        '/test/', '/tests/', 'test_', '_test.', 
        'spec.', 'spec/', '_spec.', '/spec/', '__tests__'
    ]
    
    return any(pattern in filename_lower for pattern in test_patterns)

def get_github_pull_request(pr_url: str) -> str:
    """
    Fetches the list of files changed in a GitHub pull request and returns 
    formatted text including file content.

    Args:
        pr_url (str): The URL of the GitHub pull request 
                     (e.g., https://github.com/owner/repo/pull/123).

    Returns:
        str: A formatted text string containing detailed information about each changed file,
             including the file content fetched from GitHub.
    """
    path_parts = urlparse(pr_url).path.strip("/").split("/")
    owner, repo, _, pr_number = path_parts

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"

    response = requests.get(api_url, headers=headers)

    if response.status_code != 200:
        print(f"Failed to get PR files: {response.status_code} - {response.text}")
        return "Failed to get PR files", None    

    pr = response.json()

    # Get files changes
    api_url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"

    response = requests.get(api_url, headers=headers)

    if response.status_code != 200:
        print(f"Failed to get PR files: {response.status_code} - {response.text}")
        return "Failed to fetch PR files", None
    
    result_parts = []
    
    for file_info in response.json():
        filename = file_info.get('filename', '')
        if is_test_file(filename):
            continue
        if all(k in file_info for k in ['filename', 'status', 'raw_url', 'patch']):
            # Add file metadata to formatted text output
            file_part = f"FILE: {file_info['filename']}\n"
            file_part += f"STATUS: {file_info['status']}\n"
            
            # Add patch information with proper formatting
            file_part += "PATCH:\n"
            patch = file_info['patch']
            if '\n' in patch:
                # Indent each line of the patch for better readability
                patch_lines = patch.split('\n')
                formatted_patch = '\n'.join(['    ' + line for line in patch_lines])
                file_part += formatted_patch + '\n'
            else:
                file_part += f"    {patch}\n"
            
            # Fetch and add the file content
            if file_info['status'] == 'modified':
                file_content = fetch_file_from_github_api(file_info['raw_url'])
                file_part += "SOURCE:\n"
                if file_content:
                    if '\n' in file_content:
                        # Indent each line of the source for better readability
                        content_lines = file_content.split('\n')
                        formatted_content = '\n'.join(['    ' + line for line in content_lines])
                        file_part += formatted_content + '\n'
                    else:
                        file_part += f"    {file_content}\n"
                else:
                    file_part += "    <Failed to fetch file content>\n"
                
                file_part += "===================================\n"

            result_parts.append(file_part)
    
    formatted_text = "\n".join(result_parts) if result_parts else "No files found in PR"
    
    return formatted_text, pr

def parse_arguments():
    """Parse command line arguments."""

    epilog = """
    Environment variables:
      GITHUB_ACCESS_TOKEN - Your GitHub API token
      SLACK_BOT_TOKEN - Your Slack API token
      LINEAR_API_KEY - Linear API token (optional)
      
    Note: Token environment variables are required for API access.
    """
    
    parser = argparse.ArgumentParser(
        description="Argus llm code reviewer",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument("--pull-request", 
                        action="append",
                        dest="pull_requests",
                        required=False,
                        help="URL to the GitHub pull request (can be specified multiple times)")
    
    parser.add_argument("--slack-channel",
                        required=False,
                        help="Slack channel id to fetch review requests from")

    parser.add_argument("--summary",
                        required=False,
                        help="Path to file with project summary")                        

    parser.add_argument("--linear-ticket",
                        required=False,
                        help="Linear ticket to review")

    parser.add_argument("--debug",
                        action="store_true",
                        help="Enable debug mode for llm")

    parser.add_argument("--open-vs-code",
                        action="store_true",
                        help="Clone repo, checkout branch and open vscode")
    
    return parser.parse_args()

def extract_markdown_content(text):
    """
    Extracts the content from the first markdown code block in a multiline string.
    
    Args:
        text (str): A multiline string that may contain markdown code blocks.
        
    Returns:
        str: The content between the first ```markdown and ``` tags, or None if not found.
    """
    import re
    
    # Look for content between ```markdown and ```
    match = re.search(r'```markdown\s+(.*?)```', text, re.DOTALL)
    
    if match:
        # Return just the content between the tags, without the ```markdown and ``` markers
        return match.group(1).strip()
    else:
        return text

def unwrap_urls(text):
    """
    Removes common wrappers around URLs
    Handles angle brackets, square brackets, parentheses, curly braces, and combinations of them
    Also handles cases where URLs are preceded by text like "URL:", "Link:", etc.
    
    Args:
        text (str): Text containing wrapped URLs
        
    Returns:
        str: Text with unwrapped URLs
    """
    if not text:
        return text
    
    # Regular expression to match URLs with various wrappers
    # This looks for URLs potentially wrapped in different characters
    wrapped_url_regex = r'((?:\s|^)(?:(?:URL|Link|Source|Website):\s*)?)([\[\(<{]+)(https?://[^\s\]\)>}]+)([\]\)>}]+)'
    
    # Replace wrapped URLs with just the URL itself
    result = re.sub(wrapped_url_regex, r'\1\3', text, flags=re.IGNORECASE)
    
    return result

def main():
    args = parse_arguments()
    
    pull_requests = None
    linear_ticket = None  
    post_back_to_slack = False  
    channel_id = None
    
    # 1. check if PRs are provided
    if args.pull_requests:
        pull_requests = args.pull_requests
    # 2. check if linaer ticket is provided
    elif args.linear_ticket and LINEAR_API_KEY:
        issue_json = get_linear_issues(args.linear_ticket.upper())
        linear_ticket = json.loads(issue_json)                
    # 3. if slack channel is provided, start monitoring channel for review requests
    elif args.slack_channel and SLACK_BOT_TOKEN:
        last_checked_timestamp = None
        try:
            with open(TIMESTAMP_FILE, "r") as f:
                content = f.read().strip()
                if content:
                    last_checked_timestamp = content
                    print(f"Loaded last timestamp from file: {last_checked_timestamp}")
        except FileNotFoundError:
            print("Timestamp file not found, will fetch recent history on first run.")
            last_checked_timestamp = None

        while True:
            print("\nChecking slack channel for new pull requests...")
            try: 
                client = WebClient(token=SLACK_BOT_TOKEN)
                new_messages_list, new_last_timestamp = check_new_slack_messages(client, args.slack_channel, last_checked_timestamp)                
                last_checked_timestamp = new_last_timestamp
                text_for_llm = []
                if new_messages_list and len(new_messages_list) > 0:
                    for message in reversed(new_messages_list):
                        text = message.get('text', '')
                        if text and 'bot_id' not in message:
                            text_for_llm.append(text)                            

                if len(text_for_llm) > 0:                    
                    text_for_llm = '\n'.join(text_for_llm)

                    extractor = Agent(
                        role="extract instructions for github pull request review",
                        model=Gemini(id="gemini-2.0-flash", temperature=0.2),                        
                        markdown=False,
                        instructions="Your job is to find instructions to review pull requests from github",
                        debug_mode=args.debug,
                    )
                    extractor_prompt = f"""
                    You will be provided with slack messages, please extract instructions to do code review for github pull requests.
                    Github pull requests are provided as urls, with ending like pull/<some number>.
                    Please make sure, that there is a direct request in a message to review this pull request.
                    Please remove all symbols which are wrapping urls of the github pull requests.
                    Please provide concise list of pull requests urls in a single string at the end with prefix PULL_REQUESTS_TO_REVIEW:
                    Here are slack messages:
                    {text_for_llm}
                    """
                    
                    print("Extracting pull requests...")
                    llm_pull_requests = extractor.run(extractor_prompt).content
                    pull_requests = parse_pull_requests(llm_pull_requests)
                    if pull_requests and len(pull_requests) > 0:
                        post_back_to_slack = True
                        channel_id = _find_channel_id(client, args.slack_channel, "public_channel")
                        break                    

            except Exception as e:
                print(f"Error checking slack messages: {str(e)}")   
                traceback.print_exc()

            time.sleep(30)
        
        # updating last timestamp
        try:
            with open(TIMESTAMP_FILE, "w") as f:
                f.write(last_checked_timestamp)
            print(f"\nSaved latest timestamp ({last_checked_timestamp}) to {TIMESTAMP_FILE}")
        except IOError as e:
            print(f"\nError saving timestamp to {TIMESTAMP_FILE}: {e}")
    else:
        print("Run with --help for usage information.")
        return 0        

    if linear_ticket:
        try:
            # Check if we got valid data and if there are any issues
            if (linear_ticket and "data" in linear_ticket and 
                "issues" in linear_ticket["data"] and 
                "nodes" in linear_ticket["data"]["issues"] and 
                len(linear_ticket["data"]["issues"]["nodes"]) > 0):
                
                issues = linear_ticket["data"]["issues"]["nodes"]
                tickets_to_review = [issue.get("id") for issue in issues]
        except Exception as e:
            print(f"Error checking for tickets: {str(e)}")
            exit(1)

        assistant = Agent(
            role="work with linear.app to fetch issues",
            model=Gemini(id="gemini-2.0-flash", temperature=0.2),
            tools=[
                get_linear_issue_details
            ],
            show_tool_calls=True,
            markdown=False,
            instructions="""
            Your job is to fetch detailed information from linear about the issue        
            """,
            debug_mode=args.debug,
        )        

        collection_prompt = """
        You need to fetch all relevant information from linear.app.
        """
        collection_prompt += f"""Fetch the specific issues {tickets_to_review[0]} from Linear."""
        
        collection_prompt += """
        For the issue:
        1. Get all details including title, description, comments, state, assignee and labels
        2. Clearly state the issue identifier (e.g., TEAM1-123) at the beginning of your response with prefix LINEAR_TICKET: 
        3. Find all GitHub pull request links attached to the ticket
        4. Format result in plain text.
        5. At the very end of response please provide list of github pull request links in plain text without formatting.
        """
        
        # Get data from the assistant
        print("Fetching ticket from linear...")
        collected_data = assistant.run(collection_prompt).content
        pull_requests = extract_github_links(collected_data)

        if not pull_requests:
            print("There are no github pull requests in ticket")
            return 0
    
    print("Fetching pull request(s) from github...")
    # Initialize an empty string to store all PR information
    all_pr_info = ""
    pr_jsons = []
    
    # Process each link
    for i, link in enumerate(pull_requests, 1):
        print(link)
        link = unwrap_urls(link)
        print(link)
        # Only process pull request links
        if '/pull/' in link:
            print(f"Processing PR {i}/{len(pull_requests)}: {link}")
            
            try:
                # Call get_github_pull_request for the current link
                pr_text, pr_json = get_github_pull_request(link)    
                pr_jsons.append(pr_json)
                
                # Add a header for this PR and append the information
                all_pr_info += f"\n\n{'='*50}\n"
                all_pr_info += f"PULL REQUEST: {link}\n"
                all_pr_info += f"{'='*50}\n\n"
                all_pr_info += pr_text
            except Exception as e:
                # Handle any errors that might occur
                error_message = f"Error processing {link}: {str(e)}\n"
                print(error_message)
                traceback.print_exc()
                all_pr_info = None
                break
        else:
            # If it's not a pull request link, just note it
            all_pr_info += f"\n\nNOTE: Skipped non-pull request link: {link}\n"          
    
    if not all_pr_info:
        print("Error processing pull request")
        return 0

    summary = ""
    if args.summary:
        with open(args.summary, 'r') as file:
            summary = file.read().strip()
    
    system = f"""
        Your goal is to thoroughly review code changes for security vulnerabilities.
    """
    if args.summary:
        system += f"Here is your brief technical architecture summary of the system you are working on: {summary}"

    security_reviewer = Agent(
        name="code reviewer", 
        role="You are the best in the world application security engineer which is doing top notch security code audits. Be concise and professional",
        model=Gemini(id="gemini-2.5-pro-preview-03-25", system_prompt=system),
        markdown=True,
        instructions="""
        Analyze each file change carefully, looking for:
        - Injection vulnerabilities (SQL, command, etc.)
        - Authentication/authorization issues
        - Data validation problems
        - Insecure cryptography
        - Hardcoded secrets or credentials
        - Race conditions
        - Insecure dependencies
        - Other security concerns

        Also analyse each file change carefully for any functional or logical bugs.
        
        If you find NO major security concerns, explicitly state: "NO MAJOR SECURITY CONCERNS FOUND"
        Be thorough but concise in your analysis.
        Ingore all changes in tests.
        """,
        expected_output="""
        A professional properly formatted security audit report:

        Summary
        {Title based on linear ticket title and description}
        {Brief desciption based on linear ticket description if exists} 

        {Overall summary of changes}
        ## List of changes
        {For each change provide description of what was changed, link to pull request and list to files or single file with path}
        {If there is a bug found in a change, please describe it with impact and recommendations how to fix}
        {If change was only related to how code is formatted, please flag it with FORMAT ONLY}

        ## Overall Security Assessment
        {Summary of all security findings witn accent on most important ones}

        ## Security Findings
        {For each finding pull request, file or list of files with path, vunerability class, full description in exact context, severity (if possible), how to exploit (if possible), recommendent actions to fix, code example or reference (optinoal)}

        ## Security Concerns
        {Any other security concerns without exact vulnerabilities if exists, with description, impact and reccomendations}

        ## Resume
        {Overall conclusion}

        {Optional string 'NO MAJOR SECURITY CONCERNS FOUND'}
        """,
        debug_mode=args.debug
    )

    review_prompt = """        
    Please perform a thorough security review of the following code changes.
    Focus on identifying any security vulnerabilities or concerns.
    Do proper and nice formatting of the result.
    At the very end add string 'NO MAJOR SECURITY CONCERNS FOUND' if nothing serious found.     
    Given the instructions above, rephrase and expand it to better facilitate answering, ensuring all information from the original question is retained    
    Provide only markdown report, nothing like "Ok, here is your report at the beginning. Also no need to ```markdown wrapper."
    """

    if args.linear_ticket:
        review_prompt += f"Here is the ticket and code change information: {collected_data}"
    
    review_prompt += all_pr_info
    
    # Get security analysis from the reviewer
    print("Code review...")    

    security_analysis = security_reviewer.run(review_prompt).content

    console = Console()
    md = Markdown(extract_markdown_content(security_analysis))
    console.print(md)

    if post_back_to_slack and channel_id: 
        client = WebClient(token=SLACK_BOT_TOKEN)
        response = client.files_upload_v2(
            channel=f"{channel_id}",
            content=extract_markdown_content(security_analysis).strip(), 
            filename="security_audit_report.md",
            initial_comment="Here is the Security Audit Report:",
        )

    if args.open_vs_code:
        for pr_json in pr_jsons:
            head = pr_json['head']['ref']
            base = pr_json['base']['ref']
            repo_url = pr_json['head']['repo']['ssh_url']
            repo_directory = extract_repo_name(repo_url)    

            if not os.path.isdir(repo_directory):
                subprocess.run(["git", "clone", repo_url, repo_directory], check=True)
            os.chdir(repo_directory)
            subprocess.run(["git", "fetch", "--all"], check=True)
            subprocess.run(["git", "checkout", head], check=True)
            subprocess.Popen(["code", "."])

            input("Press ENTER to continue")

    print("Done")

if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Something bad happened")
        sys.exit(1)

