# -*- coding: utf-8 -*-
"""
Trading skill base classes and SkillManager.

Skills are pluggable trading analysis modules defined in **natural language**
(YAML files). Each skill describes a common or custom trading pattern
(e.g., 龙头策略, 缩量回踩, 均线金叉) used for analysis and push notifications.

Users can write custom skills by creating a YAML file — no Python code needed.
The built-in YAML files still live under ``strategies/`` for compatibility.
"""

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

# Built-in skill YAML directory (project_root/strategies/ kept for compatibility)
_BUILTIN_SKILLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "strategies"


@dataclass
class Skill:
    """A trading skill that can be injected into the agent prompt.

    Each skill represents a common or custom trading pattern used
    for stock analysis and push notifications. Strategies are typically
    loaded from YAML files written in natural language.

    Attributes:
        name: Unique strategy identifier (e.g., "dragon_head").
        display_name: Human-readable name (e.g., "龙头策略").
        description: Brief description of when to apply this strategy.
        instructions: Detailed natural language instructions injected into the system prompt.
        category: Skill category — "trend" (趋势), "pattern" (形态), "reversal" (反转), "framework" (框架).
        core_rules: List of core trading rule numbers this strategy relates to (1-7).
        required_tools: List of tool names this skill depends on.
        allowed_tools: Optional allowlist metadata from SKILL.md frontmatter.
        aliases: Optional alias phrases used by NL selectors / bot commands.
        enabled: Whether this skill is currently active.
        source: Origin of this skill — "builtin" or file path of a custom definition.
        entrypoint: Definition file path (YAML or SKILL.md).
        bundle_dir: Skill bundle directory when loaded from SKILL.md.
        disable_model_invocation: Whether the model should avoid auto-invoking this skill.
        user_invocable: Whether the skill should be exposed in user-facing selectors.
        default_active: Whether this skill participates in the default activation set.
        default_router: Whether this skill participates in router fallback selection.
        default_priority: Ordering hint for defaults / selectors (lower comes first).
        market_regimes: Optional market regime tags used by the skill router.
        execution_context: Inline/fork execution hint from frontmatter.
        subagent_type: Optional subagent type hint from frontmatter.
        preferred_model: Optional model hint from frontmatter.
    """
    name: str
    display_name: str
    description: str
    instructions: str
    category: str = "trend"
    core_rules: List[int] = field(default_factory=list)
    required_tools: List[str] = field(default_factory=list)
    allowed_tools: List[str] = field(default_factory=list)
    aliases: List[str] = field(default_factory=list)
    enabled: bool = False
    source: str = "builtin"
    entrypoint: str = ""
    bundle_dir: str = ""
    disable_model_invocation: bool = False
    user_invocable: bool = True
    default_active: bool = False
    default_router: bool = False
    default_priority: int = 100
    market_regimes: List[str] = field(default_factory=list)
    execution_context: str = "inline"
    subagent_type: str = ""
    preferred_model: str = ""
    # Research Capability metadata.  These fields are optional for legacy
    # strategy YAML and SKILL.md bundles; old files receive safe defaults.
    version: str = "1.0.0"
    required_capabilities: List[str] = field(default_factory=list)
    risk_level: str = "low"
    evidence_policy: str = "optional"
    section_index: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""

    def __post_init__(self) -> None:
        if not self.summary:
            self.summary = self.description[:280]
        if not self.section_index:
            self.section_index = _build_section_index(self.instructions)

    def load_instructions(
        self,
        *,
        sections: Optional[Sequence[str]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        """Load selected body sections on demand and apply a character budget.

        Legacy callers already receive ``instructions`` at construction time;
        this method still provides the bounded selection seam for Research
        Capability callers and custom bundles whose entrypoint is on disk.
        """

        text = self.instructions or ""
        if sections:
            wanted = {str(item).strip().lower() for item in sections if str(item).strip()}
            chunks: List[str] = []
            current_title = ""
            current_lines: List[str] = []
            for line in text.splitlines():
                heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
                if heading:
                    if current_lines and (not wanted or current_title.lower() in wanted):
                        chunks.extend(current_lines)
                    current_title = heading.group(2).strip()
                    current_lines = [line]
                else:
                    current_lines.append(line)
            if current_lines and (not wanted or current_title.lower() in wanted):
                chunks.extend(current_lines)
            text = "\n".join(chunks).strip()
        if max_chars is not None and max_chars >= 0:
            text = text[: int(max_chars)]
        return text

    def metadata(self) -> Dict[str, Any]:
        """Return summary/capability metadata without full instructions."""

        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "summary": self.summary or self.description,
            "version": self.version,
            "category": self.category,
            "required_tools": list(self.required_tools),
            "required_capabilities": list(self.required_capabilities),
            "risk_level": self.risk_level,
            "evidence_policy": self.evidence_policy,
            "section_index": [dict(item) for item in self.section_index],
            "aliases": list(self.aliases),
            "source": self.source,
            "entrypoint": self.entrypoint,
        }


_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?(.*)$", re.DOTALL)


def _coerce_string_list(value: object) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _coerce_bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return bool(value)


def _coerce_int(value: object, default: int = 100) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


_SKILL_RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
_SKILL_EVIDENCE_POLICIES = frozenset({"optional", "required", "fact_only", "artifact_only"})


def _coerce_section_index(value: object) -> List[Dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        return [{"title": item.strip(), "level": 2} for item in value.split(",") if item.strip()]
    if not isinstance(value, list):
        raise ValueError("section_index must be a list or comma-separated string")
    result: List[Dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            title = item.strip()
            if title:
                result.append({"title": title, "level": 2})
            continue
        if not isinstance(item, Mapping) or not str(item.get("title") or "").strip():
            raise ValueError("section_index entries must contain a title")
        result.append({"title": str(item["title"]).strip(), "level": _coerce_int(item.get("level"), 2)})
    return result


def _build_section_index(instructions: str) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for line in (instructions or "").splitlines():
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            result.append({"title": match.group(2).strip(), "level": len(match.group(1))})
    return result


def _research_metadata(metadata: Mapping[str, Any], instructions: str) -> Dict[str, Any]:
    """Normalize Research Capability fields while preserving legacy defaults."""

    version = str(metadata.get("version", "1.0.0")).strip() or "1.0.0"
    risk_level = str(metadata.get("risk-level", metadata.get("risk_level", "low"))).strip().lower() or "low"
    if risk_level not in _SKILL_RISK_LEVELS:
        raise ValueError(f"invalid skill risk_level: {risk_level}")
    evidence_policy = str(
        metadata.get("evidence-policy", metadata.get("evidence_policy", "optional"))
    ).strip().lower() or "optional"
    if evidence_policy not in _SKILL_EVIDENCE_POLICIES:
        raise ValueError(f"invalid skill evidence_policy: {evidence_policy}")
    required_capabilities = _coerce_string_list(
        metadata.get("required-capabilities", metadata.get("required_capabilities"))
    )
    section_index = _coerce_section_index(
        metadata.get("section-index", metadata.get("section_index"))
    ) or _build_section_index(instructions)
    summary = str(metadata.get("summary") or metadata.get("short-description") or "").strip()
    return {
        "version": version,
        "required_capabilities": required_capabilities,
        "risk_level": risk_level,
        "evidence_policy": evidence_policy,
        "section_index": section_index,
        "summary": summary,
    }
def _parse_skill_frontmatter(raw_text: str) -> tuple[Dict[str, object], str]:
    import yaml

    match = _FRONTMATTER_RE.match(raw_text)
    if not match:
        return {}, raw_text.strip()

    metadata_raw, body = match.groups()
    metadata = yaml.safe_load(metadata_raw) or {}
    if not isinstance(metadata, dict):
        raise ValueError("Skill frontmatter must be a YAML mapping")
    return metadata, body.strip()


def _infer_skill_description(instructions: str) -> str:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", instructions or "") if part.strip()]
    if not paragraphs:
        return ""
    first = re.sub(r"\s+", " ", paragraphs[0]).strip()
    return first[:280]


def load_skill_from_yaml(filepath: Union[str, Path]) -> Skill:
    """Load a single Skill from a YAML file.

    The YAML file must contain at minimum: ``name``, ``display_name``,
    ``description``, and ``instructions``. All values are natural language text.

    Args:
        filepath: Path to the ``.yaml`` file.

    Returns:
        A ``Skill`` instance with ``enabled=False``.

    Raises:
        ValueError: If required fields are missing or the file is invalid.
        FileNotFoundError: If the file does not exist.
    """
    import yaml  # lazy import — only needed when loading skill YAML

    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Skill file not found: {filepath}")

    with open(filepath, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError(f"Invalid skill file (expected YAML mapping): {filepath}")

    # Validate required fields
    required_fields = ["name", "display_name", "description", "instructions"]
    missing = [fld for fld in required_fields if not data.get(fld)]
    if missing:
        raise ValueError(
            f"Skill file {filepath.name} missing required fields: {missing}"
        )

    research_metadata = _research_metadata(data, str(data["instructions"]).strip())
    return Skill(
        name=str(data["name"]).strip(),
        display_name=str(data["display_name"]).strip(),
        description=str(data["description"]).strip(),
        instructions=str(data["instructions"]).strip(),
        category=str(data.get("category", "trend")).strip(),
        core_rules=data.get("core_rules", []) or [],
        required_tools=data.get("required_tools", []) or [],
        allowed_tools=_coerce_string_list(data.get("allowed_tools")),
        aliases=_coerce_string_list(data.get("aliases")),
        enabled=False,
        source=str(filepath),
        entrypoint=str(filepath),
        bundle_dir=str(filepath.parent),
        disable_model_invocation=bool(data.get("disable_model_invocation", False)),
        user_invocable=bool(data.get("user_invocable", True)),
        default_active=_coerce_bool(data.get("default_active"), False),
        default_router=_coerce_bool(data.get("default_router"), False),
        default_priority=_coerce_int(data.get("default_priority"), 100),
        market_regimes=(
            _coerce_string_list(data.get("market_regimes"))
            or _coerce_string_list(data.get("market-regimes"))
        ),
        execution_context=str(data.get("context", "inline")).strip() or "inline",
        subagent_type=str(data.get("agent", "")).strip(),
        preferred_model=str(data.get("model", "")).strip(),
        **research_metadata,
    )


def load_skill_from_markdown(filepath: Union[str, Path]) -> Skill:
    """Load a single skill from a `SKILL.md` bundle entrypoint."""
    filepath = Path(filepath)
    if not filepath.exists():
        raise FileNotFoundError(f"Skill file not found: {filepath}")

    raw_text = filepath.read_text(encoding="utf-8")
    metadata, instructions = _parse_skill_frontmatter(raw_text)
    if not instructions:
        raise ValueError(f"Skill file {filepath.name} missing markdown instructions")

    skill_name = str(metadata.get("name") or filepath.parent.name).strip()
    display_name = str(
        metadata.get("display_name")
        or metadata.get("title")
        or skill_name
    ).strip()
    description = str(
        metadata.get("description")
        or _infer_skill_description(instructions)
    ).strip()
    if not skill_name or not description:
        raise ValueError(f"Skill file {filepath.name} missing required name/description")

    allowed_tools = _coerce_string_list(metadata.get("allowed-tools"))
    if not allowed_tools:
        allowed_tools = _coerce_string_list(metadata.get("allowed_tools"))
    required_tools = _coerce_string_list(metadata.get("required-tools"))
    if not required_tools:
        required_tools = _coerce_string_list(metadata.get("required_tools"))

    research_metadata = _research_metadata(metadata, instructions)
    return Skill(
        name=skill_name,
        display_name=display_name,
        description=description,
        instructions=instructions,
        category=str(metadata.get("category", "general")).strip() or "general",
        core_rules=metadata.get("core_rules", []) or [],
        required_tools=required_tools,
        allowed_tools=allowed_tools,
        aliases=_coerce_string_list(metadata.get("aliases")),
        enabled=False,
        source=str(filepath),
        entrypoint=str(filepath),
        bundle_dir=str(filepath.parent),
        disable_model_invocation=_coerce_bool(metadata.get("disable-model-invocation"), False),
        user_invocable=_coerce_bool(metadata.get("user-invocable"), True),
        default_active=_coerce_bool(
            metadata.get("default-active", metadata.get("default_active")),
            False,
        ),
        default_router=_coerce_bool(
            metadata.get("default-router", metadata.get("default_router")),
            False,
        ),
        default_priority=_coerce_int(
            metadata.get("default-priority", metadata.get("default_priority")),
            100,
        ),
        market_regimes=(
            _coerce_string_list(metadata.get("market-regimes"))
            or _coerce_string_list(metadata.get("market_regimes"))
        ),
        execution_context=str(metadata.get("context", "inline")).strip() or "inline",
        subagent_type=str(metadata.get("agent", "")).strip(),
        preferred_model=str(metadata.get("model", "")).strip(),
        **research_metadata,
    )


def load_skills_from_directory(directory: Union[str, Path]) -> List[Skill]:
    """Load all skills from YAML files in a directory.

    Scans for top-level ``*.yaml`` / ``*.yml`` compatibility files and
    nested ``SKILL.md`` bundles, sorted alphabetically.
    Skips files that fail to parse (logs a warning).

    Args:
        directory: Path to the directory containing skill definitions.

    Returns:
        List of ``Skill`` instances (all disabled by default).
    """
    directory = Path(directory)
    if not directory.is_dir():
        logger.warning(f"Skill directory does not exist: {directory}")
        return []

    skills: List[Skill] = []
    yaml_files = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    markdown_files = sorted(directory.rglob("SKILL.md"))

    for filepath in yaml_files:
        try:
            skill = load_skill_from_yaml(filepath)
            skills.append(skill)
            logger.debug(f"Loaded skill from YAML: {skill.name} ({filepath.name})")
        except Exception as e:
            logger.warning(f"Failed to load skill from {filepath.name}: {e}")

    for filepath in markdown_files:
        try:
            skill = load_skill_from_markdown(filepath)
            skills.append(skill)
            logger.debug(f"Loaded skill bundle: {skill.name} ({filepath})")
        except Exception as e:
            logger.warning(f"Failed to load skill bundle from {filepath}: {e}")

    return skills


class SkillManager:
    """Manages trading skills and generates combined prompt instructions.

    Supports loading skills from:
    1. YAML files in the built-in ``strategies/`` directory
    2. YAML files in a user-specified custom directory
    3. Programmatic ``Skill`` instances (backward compatible)

    Usage::

        manager = SkillManager()
        # Load built-in + custom skills from YAML
        manager.load_builtin_skills()
        manager.load_custom_skills("./my_skills")
        # Or register programmatically
        manager.register(some_skill)
        # Activate and generate prompt
        manager.activate(["dragon_head", "shrink_pullback"])
        instructions = manager.get_skill_instructions()
    """

    def __init__(self):
        self._skills: Dict[str, Skill] = {}

    def register(self, skill: Skill) -> None:
        """Register a skill (programmatic or YAML-loaded)."""
        self._skills[skill.name] = skill
        logger.debug(f"Registered skill: {skill.name} ({skill.display_name})")

    def load_builtin_skills(self) -> int:
        """Load all built-in skills from the compatibility `strategies/` directory.

        Returns:
            Number of skills loaded.
        """
        skills_dir = _BUILTIN_SKILLS_DIR
        if not skills_dir.is_dir():
            logger.warning(f"Built-in skill directory not found: {skills_dir}")
            return 0

        skills = load_skills_from_directory(skills_dir)
        for skill in skills:
            skill.source = "builtin"
            self.register(skill)

        logger.info(f"Loaded {len(skills)} built-in skills from {skills_dir}")
        return len(skills)

    def load_custom_skills(self, directory: Union[str, Path, None]) -> int:
        """Load custom skills from a user-specified directory.

        Custom skills override built-in ones if names conflict.

        Args:
            directory: Path to the custom skill directory.
                       If None or empty, does nothing.

        Returns:
            Number of skills loaded.
        """
        if not directory:
            return 0

        directory = Path(directory)
        if not directory.is_dir():
            logger.warning(f"Custom skill directory does not exist: {directory}")
            return 0

        skills = load_skills_from_directory(directory)
        for skill in skills:
            if skill.name in self._skills:
                logger.info(
                    f"Custom skill '{skill.name}' overrides built-in"
                )
            self.register(skill)

        logger.info(f"Loaded {len(skills)} custom skills from {directory}")
        return len(skills)

    def load_builtin_strategies(self) -> int:
        """Compatibility wrapper for older call sites."""
        return self.load_builtin_skills()

    def load_custom_strategies(self, directory: Union[str, Path, None]) -> int:
        """Compatibility wrapper for older call sites."""
        return self.load_custom_skills(directory)

    def get(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_skills(self) -> List[Skill]:
        """List all registered skills."""
        return list(self._skills.values())

    def list_active_skills(self) -> List[Skill]:
        """List only active (enabled) skills."""
        return [s for s in self._skills.values() if s.enabled]

    def get_skill_catalog(self, *, include_instructions: bool = False) -> List[Dict[str, Any]]:
        """Return a summary-first catalog for discovery endpoints/UI.

        Full skill bodies are only included when explicitly requested.  This
        keeps the default catalog small even when a workspace contains long
        SKILL.md bundles.
        """

        catalog = []
        for skill in self._skills.values():
            item = skill.metadata()
            if include_instructions:
                item["instructions"] = skill.load_instructions()
            catalog.append(item)
        return catalog

    def load_skill_content(
        self,
        name: str,
        *,
        sections: Optional[Sequence[str]] = None,
        max_chars: Optional[int] = None,
    ) -> str:
        skill = self.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill: {name}")
        return skill.load_instructions(sections=sections, max_chars=max_chars)

    def validate_required_tools(self, name: str, tool_surface: Any, profile: Any = "research_readonly") -> Dict[str, Any]:
        """Validate skill requirements against the canonical ToolSurface."""

        skill = self.get(name)
        if skill is None:
            return {"available": False, "skill": name, "reason": "skill_not_found", "missing_tools": [], "missing_capabilities": []}
        try:
            descriptors = tool_surface.describe(profile)
        except Exception as exc:
            return {
                "available": False,
                "skill": name,
                "reason": "profile_unavailable",
                "error": type(exc).__name__,
                "missing_tools": list(skill.required_tools),
                "missing_capabilities": list(skill.required_capabilities),
            }
        available_tools = {str(item.get("name")) for item in descriptors if isinstance(item, Mapping)}
        available_permissions = {
            str(permission)
            for item in descriptors
            if isinstance(item, Mapping)
            for permission in (item.get("policy", {}) or {}).get("permissions", [])
        }
        missing_tools = [tool for tool in skill.required_tools if tool not in available_tools]
        missing_capabilities = [
            capability
            for capability in skill.required_capabilities
            if not any(capability == permission or permission.startswith(f"{capability}:") for permission in available_permissions)
        ]
        reason = "available"
        if missing_tools:
            reason = "missing_tool"
        elif missing_capabilities:
            reason = "missing_capability"
        return {
            "available": not missing_tools and not missing_capabilities,
            "skill": name,
            "profile": str(profile),
            "reason": reason,
            "missing_tools": missing_tools,
            "missing_capabilities": missing_capabilities,
        }

    def activate(self, skill_names: List[str]) -> None:
        """Activate specific skills by name. Deactivate all others.

        Args:
            skill_names: List of skill names to activate.
                         If ["all"], activate everything.
        """
        if skill_names == ["all"] or "all" in skill_names:
            for s in self._skills.values():
                s.enabled = True
            logger.info(f"Activated all {len(self._skills)} skills")
            return

        for s in self._skills.values():
            s.enabled = s.name in skill_names

        activated = [s.name for s in self._skills.values() if s.enabled]
        logger.info(f"Activated skills: {activated}")

    def get_skill_instructions(self) -> str:
        """Generate combined instruction text for all active skills.

        Returns a formatted string ready to be injected into the agent
        system prompt, organized by category.
        """
        active = self.list_active_skills()
        if not active:
            return ""

        # Group by category
        categories = {"trend": "趋势", "pattern": "形态", "reversal": "反转", "framework": "框架"}
        grouped: Dict[str, List[Skill]] = {}
        for skill in active:
            cat = skill.category or "trend"
            grouped.setdefault(cat, []).append(skill)

        parts = []
        idx = 1
        # Render known categories in fixed order, then any remaining custom categories
        ordered_keys = ["trend", "pattern", "reversal", "framework"]
        for cat_key in ordered_keys + [k for k in grouped if k not in ordered_keys]:
            skills_in_cat = grouped.get(cat_key, [])
            if not skills_in_cat:
                continue
            cat_label = categories.get(cat_key, cat_key)
            parts.append(f"#### {cat_label}类技能\n")
            for skill in skills_in_cat:
                rules_ref = ""
                if skill.core_rules:
                    rules_ref = f"（关联核心理念：第{'、'.join(str(r) for r in skill.core_rules)}条）"
                support_ref = ""
                if skill.bundle_dir and skill.entrypoint.endswith("SKILL.md"):
                    support_ref = "（bundle）"
                parts.append(
                    f"### 技能 {idx}: {skill.display_name} {rules_ref}{support_ref}\n\n"
                    f"**适用场景**: {skill.description}\n\n"
                    f"{skill.load_instructions()}\n"
                )
                idx += 1

        return "\n".join(parts)

    def get_required_tools(self) -> List[str]:
        """Get all tool names required by active skills."""
        tools: set = set()
        for s in self.list_active_skills():
            tools.update(s.required_tools)
        return list(tools)
