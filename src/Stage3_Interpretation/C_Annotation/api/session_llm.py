"""Answer the pipeline's Claude prompts from an interactive Claude Code session instead of the API.

A drop-in for LLM_interface.claude_api.get_json, used with --llm_backend session. Each call is keyed by
the program (set handler.key before the call) and works in two passes over

    <session_dir>/<step>/<key>__<hash>.request.json   {"key", "system", "prompt", "schema", "answer_file"}  (written)
    <session_dir>/<step>/<key>__<hash>.answer.json    the answer: one JSON object matching "schema"         (read)

<hash> is the first 8 hex digits of sha1(system + prompt), so an answer is only ever read back for the exact
prompt it answered: a rerun with --overwrite, a changed label or new evidence writes a new request.

With images (figure_evidence.py), each image's sha256 is appended to the hashed text, and the request also lists
"images": [{"path", "sha256", "media_type"}] (path relative to the run folder holding <session_dir>, else the file
name) and an "instructions" line: the answering session opens and looks at every image before it answers.

First pass: no answer yet -> the request is written and the call returns None (pending). Someone (e.g.
Claude Code agents) writes each answer file. Second pass: the answer is validated against the same pydantic
schema the API call would use and returned. Nothing is billed to the API. The steps default to this backend
(--llm_backend session) with --session_dir <run folder>/Session_LLM.
"""

import hashlib
import json
from pathlib import Path

from pydantic import ValidationError


class session_llm:
    def __init__(self, session_dir, step, model_label="claude-code-session"):
        self.dir = Path(session_dir) / step
        self.dir.mkdir(parents=True, exist_ok=True)
        self.model_label = model_label
        self.key = None
        self.budget, self.budget_step, self.budget_reserve = (
            None,
            "",
            0.0,
        )  # same attributes as claude_api
        self.n_pending = 0
        self.last_status, self.last_error = (
            None,
            None,
        )  # of the latest call: answered | pending | invalid

    def _image_path(self, path):
        """path relative to the run folder (the parent of session_dir), else the bare file name."""
        path = Path(path)
        try:
            return (
                path.resolve().relative_to(self.dir.parent.parent.resolve()).as_posix()
            )
        except ValueError:
            return path.name

    def get_json(
        self,
        system_prompt,
        prompt,
        schema_model,
        effort="medium",
        max_tokens=4000,
        images=None,
        images_px=None,
    ):
        """images: optional [(bytes, media_type, path)]; images_px is accepted for the API signature and unused."""
        if not self.key:
            raise ValueError("set handler.key (e.g. 'P12') before each call")
        text = system_prompt + prompt
        listed = []
        for data, media_type, path in images or ():
            digest = hashlib.sha256(data).hexdigest()
            text += digest
            listed.append(
                {
                    "path": self._image_path(path),
                    "sha256": digest,
                    "media_type": media_type,
                }
            )
        name = f"{self.key}__{hashlib.sha1(text.encode()).hexdigest()[:8]}"
        answer = self.dir / f"{name}.answer.json"
        if answer.exists():
            try:
                parsed = schema_model.model_validate_json(answer.read_text())
            except ValidationError as e:
                print(f"  [session] {answer} does not match the schema: {e}")
                self.last_status, self.last_error = (
                    "invalid",
                    f"{answer.name} does not match the schema",
                )
                return None
            self.last_status, self.last_error = "answered", None
            return parsed
        request = {
            "key": name,
            "system": system_prompt,
            "prompt": prompt,
            "schema": schema_model.model_json_schema(),
            "answer_file": str(answer),
        }
        if listed:
            request["images"] = listed
            request["instructions"] = (
                "Open each image file in 'images' (paths are relative to the run folder that holds the session "
                "folder) and look at it before answering; check its sha256. Answer from what the image shows."
            )
        (self.dir / f"{name}.request.json").write_text(json.dumps(request, indent=2))
        self.n_pending += 1
        self.last_status, self.last_error = "pending", None
        print(f"  [session] {name}: request written; answer pending ({answer})")
        return None

    def report(self):
        """Closing note of a session run: how many answers are still to be written before the second pass."""
        if self.n_pending:
            print(
                f"[session] {self.n_pending} request(s) pending in {self.dir}: write each *.answer.json (e.g. with "
                f"Claude Code agents), then rerun the same command to import them"
            )
