"""Helpers for the AI-SET-2 tests (multiple named AI connections). DB-free; no network, no real credential.

Every endpoint uses the reserved ``.invalid`` top-level domain or a loopback literal and nothing here resolves or connects. Every "secret" is a sentinel.
"""

from apps.ai_gateway.adapters import workbench
from config import ai_workbench

SECRET_A = "CRED-SENTINEL-A-7e21"
SECRET_B = "CRED-SENTINEL-B-93cc"
SECRET_WORKBENCH = "WORKBENCH-SENTINEL-51aa"

CORP_ENDPOINT = "https://llm.corp.example.invalid/v1"
CLOUD_ENDPOINT = "https://api.vendor.example.invalid/v1"
LOCAL_OPENAI_ENDPOINT = "http://127.0.0.1:8000/v1"
OLLAMA_ENDPOINT = "http://127.0.0.1:11434"

REF_CORP = "AP_AI_CRED_CORP_LLM"
REF_CLOUD = "AP_AI_CRED_VENDOR"
REF_WORKBENCH = "AP_AI_CRED_WORKBENCH_B"

# The operator's side: one approved-origin list per provider (shared by every connection of the type), the company ranges and the credentials the references name.
OPERATOR_ENV = {
    "AP_AI_OPENAI_ALLOWED_ORIGINS": "https://llm.corp.example.invalid,http://127.0.0.1:8000,https://api.vendor.example.invalid",
    "AP_AI_OPENAI_COMPANY_CIDRS": "10.0.0.0/8,172.16.0.0/12",
    "AP_AI_OLLAMA_ALLOWED_ORIGINS": "http://127.0.0.1:11434",
    REF_CORP: SECRET_A,
    REF_CLOUD: SECRET_B,
    REF_WORKBENCH: SECRET_WORKBENCH,
    "AP_AI_WORKBENCH_API_KEY": SECRET_WORKBENCH,
}


def workbench_connection(**changes):
    connection = {
        "connection_id": "workbench",
        "display_name": "KPMG Workbench",
        "provider_type": workbench.PROVIDER_ID,
        "locality": "company_endpoint",
        "endpoint": None,
        "credential_ref": None,
        "enabled": True,
        "profiles": [{"profile_id": ai_workbench.PROFILE_GPT5, "model_id": workbench.DEPLOYMENT_GPT5}],
    }
    connection.update(changes)
    return connection


def corp_connection(**changes):
    connection = {
        "connection_id": "corp-llm",
        "display_name": "Company model server",
        "provider_type": "openai_compatible",
        "locality": "company_endpoint",
        "endpoint": CORP_ENDPOINT,
        "credential_ref": REF_CORP,
        "enabled": True,
        "profiles": [{"profile_id": "corp-small", "model_id": "corp-model-8b"}, {"profile_id": "corp-large", "model_id": "corp-model-70b"}],
    }
    connection.update(changes)
    return connection


def cloud_connection(**changes):
    return corp_connection(
        **{
            "connection_id": "vendor",
            "display_name": "Vendor cloud",
            "locality": "cloud",
            "endpoint": CLOUD_ENDPOINT,
            "credential_ref": REF_CLOUD,
            "profiles": [{"profile_id": "vendor-chat", "model_id": "vendor-model"}],
            **changes,
        }
    )


def local_openai_connection(**changes):
    return corp_connection(
        **{
            "connection_id": "local-server",
            "display_name": "Local server",
            "locality": "local",
            "endpoint": LOCAL_OPENAI_ENDPOINT,
            "credential_ref": None,
            "profiles": [{"profile_id": "local-chat", "model_id": "local-model:8b"}],
            **changes,
        }
    )


def ollama_connection(**changes):
    connection = {
        "connection_id": "ollama-local",
        "display_name": "Ollama on this machine",
        "provider_type": "ollama",
        "locality": "local",
        "endpoint": OLLAMA_ENDPOINT,
        "credential_ref": None,
        "enabled": True,
        "profiles": [{"profile_id": "llama", "model_id": "llama3.1:8b"}],
    }
    connection.update(changes)
    return connection


def all_connections():
    return [workbench_connection(), corp_connection(), cloud_connection(), local_openai_connection(), ollama_connection()]
