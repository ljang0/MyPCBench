"""Helpers for resolving persona registry defaults inside the harness."""

from __future__ import annotations

import json
import os
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_personas_dir(explicit_dir: str | None = None) -> Path:
    candidates = []
    if explicit_dir:
        candidates.append(Path(explicit_dir))

    env_dir = os.environ.get('PERSONAS_DIR')
    if env_dir:
        candidates.append(Path(env_dir))

    candidates.extend([
        Path('/opt/personas'),
        _repo_root() / 'personas',
    ])

    for candidate in candidates:
        if candidate.is_dir():
            return candidate

    raise FileNotFoundError('No personas directory found')


def resolve_persona_email(persona: str, explicit_dir: str | None = None) -> str:
    """Resolve the canonical auto-login email for a persona.

    Order of precedence:
      1. ``SANDBOX_LOGIN_EMAIL`` env var (what the VM was baked with — the
         single source of truth at runtime).
      2. ``personas/<persona>.json`` → ``identity.email`` if readable.
      3. Legacy fallback: ``<persona-with-dots>@sandbox.local``.

    The legacy fallback exists only so callers never crash on a totally
    missing environment; real runs always hit (1) or (2).
    """
    env_email = os.environ.get('SANDBOX_LOGIN_EMAIL')
    if env_email:
        return env_email

    try:
        personas_dir = resolve_personas_dir(explicit_dir)
        persona_path = personas_dir / f'{persona}.json'
        if persona_path.exists():
            payload = json.loads(persona_path.read_text())
            identity = payload.get('identity') or {}
            email = identity.get('email')
            if isinstance(email, str) and email:
                return email
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
        pass

    return persona.replace('_', '.') + '@sandbox.local'


def resolve_persona_identity(persona: str, explicit_dir: str | None = None) -> dict:
    """Resolve the {name, email, city} triple for a persona.

    Returns a dict with stringly-typed values, falling back to derived
    defaults when the persona JSON is unreadable. Used by agent system
    prompts to template `{PERSONA_NAME}` / `{PERSONA_EMAIL}` /
    `{PERSONA_CITY}` instead of hardcoding identity strings — keeps the
    prompt persona-agnostic so adding a new persona doesn't require a
    prompt edit.
    """
    name = persona.replace('_', ' ').title()
    email = resolve_persona_email(persona, explicit_dir)
    city = ''
    try:
        personas_dir = resolve_personas_dir(explicit_dir)
        persona_path = personas_dir / f'{persona}.json'
        if persona_path.exists():
            identity = (json.loads(persona_path.read_text()).get('identity') or {})
            if isinstance(identity.get('name'), str):
                name = identity['name']
            if isinstance(identity.get('city'), str):
                city = identity['city']
    except (FileNotFoundError, ValueError, json.JSONDecodeError, OSError):
        pass
    return {'name': name, 'email': email, 'city': city}
