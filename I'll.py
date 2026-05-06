#!/usr/bin/env python3
"""Icinga MCP Server - MCP interface for Icinga monitoring system"""

import os
import json
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
import httpx
from fastmcp import FastMCP


ICINGA_BASE_URL = os.getenv("ICINGA_BASE_URL", "http://offline-mas:8080")
API_HOST = os.getenv("ICINGA_BASE_URL_API", "https://offline-mas:8080")
API_HOST = f'{API_HOST.rsplit(":", 1)[0]}:oded.wow:5665/v1'
ICINGA_USERNAME = os.getenv("ICINGA_USERNAME", "").strip()
ICINGA_PASSWORD = os.getenv("ICINGA_PASSWORD", "").strip()

mcp = FastMCP("Icinga MCP Server")

ALL_SERVICES = '/objects/services'
HEADERS = {"Accept": "application/json"}
AUTHENTICATION = ("icingaweb", "icingaweb")


async def get_all_services(client: httpx.AsyncClient, query, *requested_fields):
    """
    Gets requested fields and returns these fields' values for all the services in Bonsai.
    """
    payload = {}
    if query:
        # payload["filter"] = f'service.name=="{query}"'
        payload["filter"] = f'match("{query}", service.name)'
    payload["attrs"] = requested_fields
    headers = HEADERS.copy()
    url = f"{API_HOST}{ALL_SERVICES}"
    headers.update({"X-HTTP-Method-Override": "GET"})
    print(url)
    print(json.dumps(payload))
    return await client.post(
        url=url,
        headers=headers,
        auth=AUTHENTICATION,
        timeout=30,
        json=payload,
    )


async def get_authenticated_client() -> httpx.AsyncClient:
    """Create an authenticated HTTP client with cookie persistence."""
    login_url = f"{ICINGA_BASE_URL}/authentication/login"

    client = httpx.AsyncClient(verify=False)

    try:
        # Get login page
        response = await client.get(login_url, timeout=30, follow_redirects=True)

        # Extract CSRF token
        response_text = await response.aread()
        text = response_text.decode()
        csrf_match = re.search(r'CSRFToken[\"\']\s*value\s*=\s*[\"\']([^\"\']+)[\"\']', text)
        csrf_token = csrf_match.group(1) if csrf_match else ""

        # Login
        login_data = {
            "username": ICINGA_USERNAME,
            "password": ICINGA_PASSWORD,
            "rememberme": "0",
            "redirect": "",
            "formUID": "form_login",
            "CSRFToken": csrf_token,
            "btn_submit": "Login",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "X-Icinga-Accept": "text/html",
            "X-Icinga-Container": "layout",
        }

        await client.post(
            login_url,
            headers=headers,
            data=login_data,
            timeout=30,
            follow_redirects=False,
        )

        return client
    except Exception:
        await client.aclose()
        raise


def extract_service_info(service_data: dict) -> dict:
    """Extract relevant information from Icinga service data.

    Icinga API returns deeply nested data. This function extracts the most
    important fields into a simpler structure while preserving all data.
    """
    result = {
        # "full_data": service_data,
        "extracted": {
            "service_name": service_data.get("name") or service_data.get("display_name"),
            "host_name": service_data.get("host", {}).get("name"),
            "state": {
                "hard_state": service_data.get("state", {}).get("hard_state"),
                "soft_state": service_data.get("state", {}).get("soft_state"),
                "state_type": service_data.get("state", {}).get("state_type"),
                "severity": service_data.get("state", {}).get("severity"),
            },
            "output": service_data.get("state", {}).get("output"),
            "long_output": service_data.get("state", {}).get("long_output"),
            "check_info": {
                "last_update": service_data.get("state", {}).get("last_update"),
                "last_state_change": service_data.get("state", {}).get("last_state_change"),
                "next_check": service_data.get("state", {}).get("next_check"),
                "check_commandline": service_data.get("state", {}).get("check_commandline"),
                "execution_time": service_data.get("state", {}).get("execution_time"),
            },
            "status_flags": {
                "is_problem": service_data.get("state", {}).get("is_problem"),
                "is_handled": service_data.get("state", {}).get("is_handled"),
                "is_acknowledged": service_data.get("state", {}).get("is_acknowledged"),
                "in_downtime": service_data.get("state", {}).get("in_downtime"),
                "is_reachable": service_data.get("state", {}).get("is_reachable"),
            },
        }
    }
    return result


@mcp.tool()
async def icinga_get_service_status(service_name: str, host_name: str = "icinga") -> str:
    """Get the current status of a service in Icinga.

    Args:
        service_name: The full name of the service (e.g., "aqua.Shaloms-150.Sonia.Shalom_is_Down.32948")
        host_name: The host name (default: "icinga")

    Returns:
        JSON string with service status information including:
        - service_name: Service name
        - host_name: Host name
        - state: Current state (0=OK, 1=WARNING, 2=CRITICAL, 3=UNKNOWN)
        - state_type: State type (SOFT/HARD)
        - output: Service output/check result
        - last_update: Timestamp of last check
        - last_state_change: When the state last changed
        - next_check: Timestamp of next scheduled check
        - is_problem: Whether this is a problem state
        - is_handled: Whether the problem is handled (acknowledged or in downtime)
        - is_acknowledged: Whether the service is acknowledged
        - in_downtime: Whether service is in downtime

    Example:
        icinga_get_service_status("aqua.Shaloms-150.Sonia.Shalom_is_Down.32948")
    """
    client = await get_authenticated_client()
    try:
        url = f"{ICINGA_BASE_URL}/icingadb/services"
        response = await client.get(f'{url}?format=json&host.name={host_name}&name={service_name}', timeout=30, follow_redirects=False)
        response.raise_for_status()
        result = response.json()

        # The API returns an array, take first match if available
        if isinstance(result, list) and len(result) > 0:
            # Extract and format the service info
            return json.dumps(extract_service_info(result[0]), ensure_ascii=False, indent=2)
        else:
            return json.dumps({"error": "Service not found", "service_name": service_name, "host_name": host_name}, indent=2)
    finally:
        await client.aclose()


@mcp.tool()
async def icinga_get_service_history(service_name: str, host_name: str = "icinga") -> str:
    """Get the historical event log for a specific Icinga service.

    Args:
        service_name: The full service name (e.g., "aqua.Shaloms-150.Sonia.Shalom_is_Down.32948")
        host_name: The host name (default: "icinga")
        limit: Maximum number of history entries (default: 20)
    Returns:
        JSON string with service history including status, service_name, host_name, and history_content
    Example:
        icinga_get_service_history("aqua.Shaloms-150.Sonia.Shalom_is_Down.32948", "icinga")
    IMPORTANT: Always print it nicely to the user with colors (green and red) and as a table, not as raw text.
    """
    client = await get_authenticated_client()
    url = (
        f"{ICINGA_BASE_URL}/icingadb/service/history"
        f"?name={service_name}&limit=20"
        f"&host.name={host_name}"
    )
    try:
        # GET the history page with authenticated session
        response = await client.get(url)
        response.raise_for_status()

        # Parse HTML with BeautifulSoup
        soup = BeautifulSoup(response.text, 'html.parser')
        content_div = soup.find('div', class_='content full-width')

        if content_div:
            # Extract text content from the div
            history_text = content_div.get_text(separator='\n', strip=True)

            # Parse the history entries
            history_lines = [line.strip() for line in history_text.split('\n') if line.strip()]

            return json.dumps({
                "status": "success",
                "service_name": service_name,
                "host_name": host_name,
                "history_content": history_lines,
                "raw_text": history_text[:5000],  # Limit raw text size
            }, ensure_ascii=False, indent=2)
        else:
            # Fallback: return title for debugging
            page_title = soup.title.string if soup.title else "No title"
            return json.dumps({
                "status": "error",
                "message": "Could not find 'content full-width' div",
                "debug": {
                    "page_title": page_title,
                    "url": url,
                    "response_length": len(response.text),
                }
            }, indent=2)

    except httpx.HTTPError as e:
        return json.dumps({"status": "error", "message": f"HTTP error: {str(e)}"}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    finally:
        await client.aclose()


@mcp.tool()
async def icinga_get_service(query: str, host_name: str = "icinga") -> str:
    """Search for services by specific name.

    Args:
        query: Specific service name
        host_name: The host name (default: "icinga")

    Returns:
        JSON string with matching service and it's current status

    Example:
        icinga_search_services("aqua.Shaloms-150")
        icinga_search_services("Sonia")
    """
    client = await get_authenticated_client()
    try:
        url = f"{ICINGA_BASE_URL}/icingadb/services"
        params = {
            "name": query,
            "host.name": host_name,
            "format": "json"
        }
        print(f'{url}?format=json&host.name={host_name}&name={query}')
        response = await client.get(f'{url}?format=json&host.name={host_name}&name={query}', timeout=30, follow_redirects=False)
        response.raise_for_status()
        result = response.json()

        # Process the array of services
        if isinstance(result, list):
            processed = []
            for service in result:
                processed.append(extract_service_info(service))
            return json.dumps({
                "count": len(processed),
                "query": query,
                "services": processed
            }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({"error": "Invalid response format", "result": result}, indent=2)
    finally:
        await client.aclose()


@mcp.tool()
async def icinga_search_services(query: str) -> str:
    """Search for services by name pattern.

    Args:
        query: pattern

    Returns:
        JSON string with matching services and their current status

    Example:
        icinga_search_services("aqua.Shaloms-150")
        icinga_search_services("Sonia")
    """
    client = await get_authenticated_client()
    try:
        response = await get_all_services(client, query, "display_name")
        response.raise_for_status()
        result = response.json()

        # Process the array of services
        if isinstance(result, dict) and 'results' in result:
            processed = []
            for service in result['results']:
                name = service.get('attrs', {}).get("display_name")
                if name:
                    processed.append(name)
            return json.dumps({
                "count": len(processed),
                "query": query,
                "services": processed
            }, ensure_ascii=False, indent=2)
        else:
            return json.dumps({"error": "Invalid response format", "result": result}, indent=2)
    finally:
        await client.aclose()


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=8001)
