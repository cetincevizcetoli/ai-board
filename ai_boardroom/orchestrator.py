import re
import queue
import threading
import json
import time
from datetime import datetime, timezone
from . import api_transport
from copy import deepcopy

from PyQt6.QtCore import QThread, pyqtSignal

from .session_log import build_user_objection_report, save_transcript_json, save_summary_md
from . import routing
from .routing import RoutePolicy, PolicyError, RoutesExhausted, DailyQuotaExhausted, EmptyResponseError, error_kind, retry_after_seconds

# --- Ayarlanabilir sabitler ---
CALL_DELAY_SECONDS = 3       # her API cagrisindan once bu kadar bekle (free-tier limitine takilmamak icin)
MAX_RETRIES = 2               # rate-limit hatasinda ayni saglayiciyi kac kez daha dene
RETRY_BACKOFF_SECONDS = 10    # yeniden denemeden once ek bekleme
SUMMARY_FALLBACK_ORDER = ["gemini", "groq", "openai", "anthropic", "openrouter"]

RATE_LIMIT_MARKERS = ["429", "rate limit", "ratelimit", "quota", "resource_exhausted", "resource exhausted"]

DEFAULT_MODELS = {
    "gemini": "gemini-2.0-flash",
    "anthropic": "claude-3-5-sonnet-20241022",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "openrouter": "inclusionai/ling-3.2-flash:free",
}

PROVIDER_LABELS = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT)",
    "gemini": "Google (Gemini)",
    "groq": "Groq (Llama vb.)",
    "openrouter": "OpenRouter (Cesitli)",
}

# Persona cevabinin sonuna eklenen, kullaniciya gosterilmeyen yapilandirilmis blok.
# Bu sayede ekstra bir API cagrisi yapmadan (ayni cevabin icinde) Issue/Decision
# takibi yapabiliyoruz.
META_BLOCK_MARKER = "---META---"
META_KEYS = {
    "ACIK": "acik", "AÇIK": "acik", "COZULDU": "cozuldu", "ÇÖZÜLDÜ": "cozuldu",
    "KARAR": "karar", "IPTAL": "iptal", "İPTAL": "iptal",
    "SORU": "soru", "YANIT": "yanit", "GORUS": "gorus", "GÖRÜŞ": "gorus",
    "ITIRAZ": "itiraz", "İTİRAZ": "itiraz",
    "ITIRAZ_COZULDU": "itiraz_cozuldu", "İTİRAZ_ÇÖZÜLDÜ": "itiraz_cozuldu",
    "DESTEK": "destek", "KARSI": "karsi", "KARŞI": "karsi",
    "MUDAHALE_YANITI": "mudahale_yaniti", "MÜDAHALE_YANITI": "mudahale_yaniti",
}
META_LINE_RE = re.compile(r"^(" + "|".join(META_KEYS) + r")\s*:\s*(.*)$", re.IGNORECASE)
ROLE_ROUTE_FAILURES = {
    "empty_response", "response_timeout", "upstream_rate_limit", "rate_limit",
    "user_skip", "model_unavailable", "temporary", "failure",
}


class DiscussionStopped(Exception):
    pass


class SummaryFailed(RuntimeError):
    """Public error with a fixed message; SDK errors may contain credentials."""


class PinnedRoleUnavailable(RoutesExhausted):
    """A fixed participant missed its turn while the other roles can still run."""


class DiscussionWorker(QThread):
    message_ready = pyqtSignal(dict)
    status = pyqtSignal(str)
    round_paused = pyqtSignal(int)
    finished_all = pyqtSignal(str, list, str)
    error = pyqtSignal(str)
    session_ended = pyqtSignal(str)
    state_changed = pyqtSignal(dict)
    routing_event = pyqtSignal(dict)

    def __init__(self, project, personas, rounds, api_keys, pending_user_msgs,
                 summary_provider=None, summary_model=None, session_path=None,
                 routing_config=None, catalog_loader=None, pacer=None, request_runner=None, quota_loader=None):
        super().__init__()
        self.project = project
        self.personas = deepcopy(personas)
        self.rounds = rounds
        self.api_keys = dict(api_keys)
        self.pending_user_msgs = pending_user_msgs
        self.summary_provider = summary_provider
        self.summary_model = summary_model
        self.is_running = True
        self.is_paused = False
        self.transcript = []
        self.session_path = session_path
        self.session_status = "running"
        self.completed_rounds = 0
        self.round_failures = []
        self.model_handoffs = []
        self._pending_handoff = None
        self._retrying_round = False
        self.summary_text = ""
        self.summary_source = ""
        self.last_error = ""
        self._stop_event = threading.Event()
        self._skip_event = threading.Event()
        self._network_active = False
        self._request_sequence = 0
        self._current_request_id = None
        self._request_runner = request_runner or api_transport.run_request
        self._lock = threading.RLock()
        self.accepting_messages = True
        self.user_instructions = []
        self.user_objections = []
        self._previous_round_snapshot = None
        self._offered_objection_ids = set()
        self.unresolved_references = []
        self.questions = []
        self.opinions = []
        self.objections = []
        self.votes = []
        self.meta_warnings = []
        self.api_calls = []
        self.api_errors = []
        self._last_call_details = {}
        self.route_policy = RoutePolicy(routing_config, catalog_loader)
        self.routing_config = self.route_policy.config
        self.pacer = pacer or routing.PACER
        self.routing_events = []
        self._last_response_info = {}
        self._request_context = {}
        self._active_route = {}
        self._model_cooldowns = {}
        self._failed_role_routes = {}
        self.role_models = {}
        self.free_quota = None
        self._quota_checked = False
        self._quota_loader = quota_loader or routing.fetch_free_quota

        # --- Issue / Decision takibi ---
        # Ham transkripti tekrar tekrar modele gondermek yerine, her turda
        # acilan/kapanan konularin ve alinan kararlarin kucuk, yapilandirilmis
        # bir ozetini tutuyoruz; tum onceki konusmalar yeniden gonderilmiyor.
        self.issues = []      # [{id, baslik, durum, acan, tur}]
        self.decisions = []   # [{id, ozet, tur}]
        self._issue_counter = 0
        self._decision_counter = 0

    def snapshot(self):
        with self._lock:
            return deepcopy({
                "schema_version": 2, "participants": self.personas,
                "status": self.session_status, "rounds": self.rounds,
                "completed_rounds": self.completed_rounds,
                "round_failures": self.round_failures,
                "model_handoffs": self.model_handoffs,
                "issues": self.issues, "decisions": self.decisions,
                "user_instructions": self.user_instructions,
                "user_objections": self.user_objections,
                "unresolved_references": self.unresolved_references,
                "questions": self.questions, "opinions": self.opinions,
                "objections": self.objections, "votes": self.votes,
                "meta_warnings": self.meta_warnings,
                "assessments": self.decision_assessments(),
                "summary": self.summary_text, "summary_source": self.summary_source,
                "last_error": self.last_error,
                "api_calls": self.api_calls,
                "api_errors": self.api_errors,
                "usage_mode": self.routing_config["mode"],
                "routing_events": self.routing_events,
                "catalog_checked_at": self.route_policy.catalog_checked_at,
                "role_models": self.role_models, "free_quota": self.free_quota,
            })

    def checkpoint(self):
        with self._lock:
            state = self.snapshot()
            if self.session_path:
                save_transcript_json(self.session_path, self.project, self.transcript, **state)
            self.state_changed.emit(state)

    def _record(self, entry):
        with self._lock:
            entry.setdefault("message_id", f"M{len(self.transcript) + 1}")
            self.transcript.append(entry)
            try:
                self.checkpoint()
            except Exception:
                # Keep even a failed-to-save message visible and available to manual export.
                self.message_ready.emit(entry)
                raise
        self.message_ready.emit(entry)

    def submit_user_message(self, text, *, objection=False):
        with self._lock:
            if not self.accepting_messages or self._stop_event.is_set():
                return False
            self.user_instructions.append(text)
            round_number = min(self.completed_rounds + 1, self.rounds)
            entry = {"speaker": "Sen", "round": round_number, "text": text}
            command = None if objection else re.fullmatch(r"(?:İPTAL|IPTAL)\s*:\s*(D\d+)", text.strip(), re.I)
            if command:
                self._apply_meta({"iptal": [command[1]]}, "Sen", self.completed_rounds + 1)
            if objection:
                objection_id = f"U{len(self.user_objections) + 1}"
                entry["user_objection_id"] = objection_id
                self.user_objections.append({"id": objection_id, "text": text, "round": round_number,
                                             "message_id": f"M{len(self.transcript) + 1}", "replies": []})
            self._record(entry)
            self.pending_user_msgs.put(f"[{objection_id}] {text}" if objection else text)
            return True

    def _drain_user_messages(self, context):
        while True:
            try:
                text = self.pending_user_msgs.get_nowait()
            except queue.Empty:
                return
            context.append(f"Sen (Kullanıcı): {text}")

    def run(self):
        try:
            self.checkpoint()
            for r in range(1, self.rounds + 1):
                self._check_stop()
                self.status.emit(f"Tur {r} yürütülüyor...")
                with self._lock:
                    self._previous_round_snapshot = (r, self._previous_round_context(r))
                round_context = []
                self._run_round(r, round_context)
                self.completed_rounds = r
                self.checkpoint()
                if r < self.rounds:
                    self._pause_round(r, "paused")
            with self._lock:
                self._check_stop()
                self.accepting_messages = False
                if not self.pending_user_msgs.empty():
                    self.session_status = "review_required"
                    self.status.emit("Son konuşmadan sonra müdahale geldi; nihai rapor oluşturulmadı.")
                    return
                self.session_status = "summarizing"
                self.checkpoint()
            summary, prov, model = self.synthesize(self.summary_provider, self.summary_model)
            self._check_stop()
            source = f"{PROVIDER_LABELS.get(prov, prov)} · {model}"
            self.save_summary_result(summary, source)
            self.finished_all.emit(summary, self.transcript, source)
        except DiscussionStopped:
            self.session_status = "stopped"
            self.status.emit("Durduruldu. Tamamlanan mesajlar korundu; oturum eksik.")
        except SummaryFailed as exc:
            self.session_status = "summary_failed"
            self.last_error = str(exc)
            self.error.emit(self.last_error)
        except (PolicyError, RoutesExhausted, DailyQuotaExhausted) as exc:
            self.session_status = "failed"
            self.last_error = str(exc)
            self.error.emit(self.last_error)
        except Exception as exc:
            self.session_status = "summary_failed" if self.completed_rounds == self.rounds else "failed"
            self.last_error = f"İşlem başarısız ({type(exc).__name__}); tamamlanan mesajlar korundu."
            self.error.emit(self.last_error)
        finally:
            self.accepting_messages = False
            try:
                self.checkpoint()
                if self.session_path and self.session_status != "completed":
                    from .session_log import save_incomplete_report
                    save_incomplete_report(self.session_path, self.project, self.transcript, self.snapshot())
            except Exception:
                self.session_status = "save_failed"
                self.error.emit("Kayıt diske yazılamadı. Uygulamayı kapatmadan konuşmayı dışa aktarın.")
            self.session_ended.emit(self.session_status)

    def _pause_round(self, round_number, status):
        self._check_stop()
        self.is_paused = True
        self.session_status = status
        self.checkpoint()
        self.round_paused.emit(round_number)
        while self.is_paused:
            self._check_stop()
            self._stop_event.wait(0.05)
        self._check_stop()
        self.session_status = "running"
        self.checkpoint()

    def _run_round(self, round_number, context):
        pending = self.personas
        handoff = None
        self._retrying_round = False
        while pending:
            missing = []
            for persona in pending:
                self._check_stop()
                speaker = persona["name"]
                if handoff and speaker != handoff["speaker"]:
                    missing.append(persona)
                    continue
                try:
                    self._speak(persona, context, round_number, handoff)
                except PinnedRoleUnavailable as exc:
                    missing.append(persona)
                    with self._lock:
                        self.round_failures.append({"round": round_number, "speaker": speaker,
                                                    "model": self.role_models[speaker]["actual_model"],
                                                    "reason": str(exc), "recovered_by": None,
                                                    "handoff_target": handoff["model"] if handoff else None})
                        self.checkpoint()
                    if not self._retrying_round:
                        context.append(f"{speaker}: Bu turdaki yanıt henüz alınamadı. Önceki görüşü ortak kayıttadır.")
                    self.status.emit(f"Tur {round_number} · {speaker} yanıt veremedi. Kalan katılımcılar kontrol ediliyor.")
            if missing:
                self._pause_round(round_number, "round_incomplete")
                self._retrying_round = True
                with self._lock:
                    handoff, self._pending_handoff = self._pending_handoff, None
            pending = missing
        self._retrying_round = False

    def _speak(self, persona, context, round_number, handoff=None):
        self._last_response_info = {}
        self._offered_objection_ids = set()
        raw_resp = (self.call_llm(persona, context, round_number, handoff=handoff)
                    if handoff else self.call_llm(persona, context, round_number))
        if not raw_resp or not raw_resp.strip():
            raise RuntimeError("Sağlayıcı boş cevap döndürdü.")
        clean_text, meta = self._parse_meta_block(raw_resp)
        speaker = persona["name"]
        provider = persona["provider"]
        requested_model = persona.get("model") or DEFAULT_MODELS[provider]
        with self._lock:
            message_id = f"M{len(self.transcript) + 1}"
            route = self._last_response_info
            transfer = None
            if handoff:
                previous = self.role_models[speaker]
                replacement = {"provider": route["provider"], "route_model": handoff["model"], "actual_model": route["model"]}
                transfer = {"round": round_number, "speaker": speaker, "from": dict(previous), "to": replacement,
                            "message_id": message_id}
                self.model_handoffs.append(transfer)
                self.role_models[speaker] = replacement
                provider, requested_model = handoff["provider"], handoff["model"]
            if not meta:
                self.meta_warnings.append(f"Tur {round_number}, {speaker}: META yok veya okunamadı; görüş takibi eksik olabilir.")
            opinion_count = len(self.opinions)
            self._apply_meta(meta, speaker, round_number)
            if meta and len(self.opinions) == opinion_count:
                self.meta_warnings.append(f"Tur {round_number}, {speaker}: geçerli GÖRÜŞ kaydı yok; görüş takibi eksik.")
            self._apply_user_objection_replies(meta, speaker, round_number, message_id)
            for failure in self.round_failures:
                if failure["round"] == round_number and failure["speaker"] == speaker and not failure["recovered_by"]:
                    failure["recovered_by"] = message_id
            self._record({"speaker": speaker, "round": round_number, "text": clean_text, "message_id": message_id,
                          "provider": route.get("provider", provider),
                          "model": route.get("model", self._actual_model(provider, requested_model)),
                          "requested_provider": provider, "requested_model": requested_model,
                          "fallback_used": route.get("fallback_used", False), "route_attempts": route.get("attempts", []),
                          **({"model_handoff": transfer} if transfer else {})})
        takeover = f" [Model devri: {transfer['from']['actual_model']} → {transfer['to']['actual_model']}]" if transfer else ""
        context.append(f"[{message_id}] {speaker} ({persona['role']}){takeover}: {clean_text}")
        self._check_stop()

    def _check_stop(self):
        if self._stop_event.is_set():
            raise DiscussionStopped()

    def resume(self):
        with self._lock:
            self.is_paused = False

    def handoff_targets(self):
        with self._lock:
            if not self.is_paused or self.session_status != "round_incomplete" or self._stop_event.is_set():
                return []
            return list(dict.fromkeys(f["speaker"] for f in self.round_failures
                                      if f["round"] == self.completed_rounds + 1 and not f["recovered_by"]))

    def handoff_options(self, speaker, entries):
        with self._lock:
            if speaker not in self.handoff_targets():
                return []
            tried = {(e.get("provider"), e.get("model")) for e in self.routing_events if e.get("event") == "attempt"}
            tried.update((call["provider"], call["actual_model"]) for call in self.api_calls)
            previous = self.role_models[speaker]
            tried.add((previous["provider"], previous["actual_model"]))
            mode = self.routing_config["mode"]
            return [entry for entry in entries
                    if entry.get("id") and (entry["provider"], entry["id"]) not in tried
                    and entry["id"] not in ("openrouter/free", "openrouter/auto")
                    and self.api_keys.get(entry["provider"])
                    and (entry["provider"] == "openrouter" and entry.get("tier") == "free" if mode == "free"
                         else self.routing_config.get("paid_enabled") and entry.get("tier") != "free")]

    def request_handoff(self, speaker, entry):
        with self._lock:
            if not self.handoff_options(speaker, [entry]):
                return False
            self._pending_handoff = {"speaker": speaker, "provider": entry["provider"], "model": entry["id"]}
            self.is_paused = False
            return True

    def stop(self):
        self.is_running = False
        self.accepting_messages = False
        self._stop_event.set()
        self.is_paused = False

    def save_summary_result(self, summary, source):
        if self.session_path:
            save_summary_md(self.session_path, self.project, self.transcript, summary, source, state=self.snapshot())
        self.summary_text, self.summary_source = summary, source
        self.session_status = "completed"
        self.last_error = ""
        self.checkpoint()

    def generate_summary_only(self, provider=None, model=None):
        if self.completed_rounds != self.rounds or self.session_status == "review_required":
            raise RuntimeError("Tüm turlar tamamlanmadan nihai rapor üretilemez.")
        summary, prov, mdl = self.synthesize(provider, model)
        return summary, f"{PROVIDER_LABELS.get(prov, prov)} · {mdl}"

    # ------------------------------------------------------------------
    # Meta blok ayristirma / uygulama
    # ------------------------------------------------------------------
    def _parse_meta_block(self, response_text):
        """Persona cevabinin sonundaki ---META--- blogunu ayirir.
        Doner: (kullaniciya gosterilecek temiz metin, meta dict)
        """
        if not response_text:
            return "", {}
        has_marker = META_BLOCK_MARKER in response_text
        clean_part, _, meta_part = response_text.partition(META_BLOCK_MARKER)
        # Real models sometimes put valid structured lines before the delimiter.
        # Preserve those fields instead of silently losing negotiation evidence.
        clean_lines, structured_lines = [], []
        for line in clean_part.splitlines():
            normalized = line.strip().removeprefix("- ").replace("**", "")
            if META_LINE_RE.match(normalized):
                structured_lines.append(normalized)
            else:
                clean_lines.append(line)
        if not has_marker and not structured_lines:
            return response_text.strip(), {}
        meta = {}
        if not has_marker:
            meta["warnings"] = ["META ayırıcı yok; açık yapılandırılmış alanlar okundu."]
        for line in structured_lines + meta_part.splitlines():
            normalized = line.strip().removeprefix("- ").replace("**", "")
            match = META_LINE_RE.match(normalized)
            if match:
                key = match[1].upper().translate(str.maketrans("ÇĞİÖŞÜ", "CGIOSU"))
                meta.setdefault(META_KEYS[key], []).append(match[2].strip())
            elif line.strip() and not line.strip().startswith("```") and line.strip() != META_BLOCK_MARKER:
                meta.setdefault("warnings", []).append("Okunamayan META satırı: " + line.strip())
        return "\n".join(clean_lines).strip(), meta

    @staticmethod
    def _values(meta, key):
        values = meta.get(key, [])
        if isinstance(values, str):
            values = [values]
        return [v.strip() for v in values if v.strip().lower() not in ("yok", "-", "")]

    @staticmethod
    def _reference(records, value, field):
        token = value.split("|", 1)[0].strip().lstrip("#")
        by_id = [item for item in records if item["id"].casefold() == token.casefold()]
        if by_id:
            return by_id[0]
        exact = [item for item in records if item[field].casefold() == token.casefold()]
        return exact[0] if len(exact) == 1 else None

    def _apply_meta(self, meta, persona_name, current_round):
        self.meta_warnings.extend(f"{persona_name}: {warning}" for warning in meta.get("warnings", []))
        for title in self._values(meta, "acik"):
            if not any(i["baslik"].casefold() == title.casefold() for i in self.issues):
                self._issue_counter += 1
                self.issues.append({"id": f"I{self._issue_counter}", "baslik": title,
                                    "durum": "AÇIK", "acan": persona_name, "tur": current_round})
        for value in self._values(meta, "cozuldu"):
            issue = self._reference(self.issues, value, "baslik")
            if issue:
                issue["durum"] = "ÇÖZÜLDÜ"
            else:
                self.unresolved_references.append({"type": "cozuldu", "value": value, "speaker": persona_name})
        for text in self._values(meta, "karar"):
            if not any(d["ozet"].casefold() == text.casefold() for d in self.decisions):
                self._decision_counter += 1
                self.decisions.append({"id": f"D{self._decision_counter}", "ozet": text,
                                       "tur": current_round, "durum": "AKTİF", "proposer": persona_name})
        for value in self._values(meta, "iptal"):
            decision = self._reference(self.decisions, value, "ozet")
            if decision:
                decision["durum"] = "İPTAL"
                decision["cancelled_by"] = persona_name
                decision["cancellation"] = value
            else:
                self.unresolved_references.append({"type": "iptal", "value": value, "speaker": persona_name})
        self._apply_negotiation(meta, persona_name, current_round)

    def _apply_negotiation(self, meta, speaker, current_round):
        names = [p["name"] for p in self.personas]
        revision = self._model_revision(speaker)
        actual_model = self.role_models.get(speaker, {}).get("actual_model")
        for value in self._values(meta, "soru"):
            parts = value.split("|", 1)
            target = next((name for name in names if name.casefold() == parts[0].strip().casefold()), None)
            if len(parts) != 2 or not target or target == speaker or not parts[1].strip():
                self.meta_warnings.append(f"{speaker}: geçersiz soru hedefi/biçimi: {value}")
                continue
            if not any(q["from"] == speaker and q["to"] == target and q["text"] == parts[1].strip()
                       and q["status"] == "open" for q in self.questions):
                self.questions.append({"id": f"Q{len(self.questions) + 1}", "from": speaker,
                                       "to": target, "text": parts[1].strip(), "round": current_round,
                                       "status": "open", "answer": ""})
        for value in self._values(meta, "yanit"):
            parts = value.split("|", 1)
            question = self._reference(self.questions, parts[0].strip(), "text")
            if len(parts) == 2 and parts[1].strip() and question and question["to"] == speaker and question["status"] == "open":
                question.update(status="answered", answer=parts[1].strip(), answered_by=speaker, answered_round=current_round)
            else:
                self.meta_warnings.append(f"{speaker}: yanıt eşleştirilemedi: {value}")
        for value in self._values(meta, "gorus"):
            parts = [part.strip() for part in value.split("|", 2)]
            if len(parts) != 3 or not all(parts):
                self.meta_warnings.append(f"{speaker}: görüş için konu, görüş ve gerekçe gerekli: {value}")
                continue
            topic, stance, reason = parts
            previous = next((o for o in reversed(self.opinions)
                             if o["speaker"] == speaker and o.get("model_revision", 0) == revision
                             and o["topic"].casefold() == topic.casefold()), None)
            self.opinions.append({"speaker": speaker, "topic": topic, "stance": stance,
                                  "reason": reason, "round": current_round,
                                  "model_revision": revision, "model": actual_model,
                                  "previous": previous["stance"] if previous else "",
                                  "changed": bool(previous and previous["stance"].casefold() != stance.casefold())})
        for value in self._values(meta, "itiraz"):
            parts = value.split("|", 1)
            decision = self._reference(self.decisions, parts[0].strip(), "ozet")
            if len(parts) != 2 or not parts[1].strip() or not decision or decision["durum"] == "İPTAL":
                self.meta_warnings.append(f"{speaker}: itiraz eşleştirilemedi: {value}")
                continue
            if any(o["decision_id"] == decision["id"] and o["speaker"] == speaker
                   and o.get("model_revision", 0) == revision
                   and o["reason"] == parts[1].strip() and o["status"] == "open" for o in self.objections):
                continue
            self.objections.append({"id": f"O{len(self.objections) + 1}", "decision_id": decision["id"],
                                    "speaker": speaker, "reason": parts[1].strip(), "round": current_round,
                                    "status": "open", "model_revision": revision})
        for value in self._values(meta, "itiraz_cozuldu"):
            parts = value.split("|", 1)
            objection = self._reference(self.objections, parts[0].strip(), "reason")
            if (len(parts) == 2 and parts[1].strip() and objection and objection["speaker"] == speaker
                    and objection.get("model_revision", 0) == revision):
                objection.update(status="resolved", resolution=parts[1].strip(), resolved_round=current_round)
            else:
                self.meta_warnings.append(f"{speaker}: yalnızca kendi itirazını gerekçeyle kapatabilir: {value}")
        for key, stance in (("destek", "support"), ("karsi", "oppose")):
            for value in self._values(meta, key):
                parts = value.split("|", 1)
                decision = self._reference(self.decisions, parts[0].strip(), "ozet")
                if len(parts) != 2 or not parts[1].strip() or not decision or decision["durum"] == "İPTAL" or speaker not in names:
                    self.meta_warnings.append(f"{speaker}: destek/karşı görüş eşleştirilemedi: {value}")
                    continue
                self.votes.append({"decision_id": decision["id"], "speaker": speaker, "stance": stance,
                                   "reason": parts[1].strip(), "round": current_round, "model_revision": revision,
                                   "model": actual_model})

    def _model_revision(self, speaker):
        return sum(item["speaker"] == speaker for item in self.model_handoffs)

    def decision_assessments(self):
        assessments = []
        names = {p["name"] for p in self.personas}
        revisions = {name: self._model_revision(name) for name in names}
        for decision in self.decisions:
            if decision["durum"] == "İPTAL":
                continue
            latest = {v["speaker"]: v for v in self.votes if v["decision_id"] == decision["id"]
                      and v.get("model_revision", 0) == revisions.get(v["speaker"])}
            supporters = [n for n in sorted(names) if n in latest and latest[n]["stance"] == "support"
                          and latest[n]["round"] == self.rounds]
            opponents = [v for v in latest.values() if v["stance"] == "oppose"]
            objections = [o for o in self.objections if o["decision_id"] == decision["id"] and o["status"] == "open"]
            assessments.append({"id": decision["id"], "text": decision["ozet"],
                                "consensus": bool(names and set(supporters) == names and not opponents and not objections),
                                "supporters": supporters, "opponents": opponents, "objections": objections,
                                "missing_final_support": sorted(names - set(supporters))})
        return assessments

    def _apply_user_objection_replies(self, meta, speaker, round_number, message_id):
        for value in self._values(meta, "mudahale_yaniti"):
            identifier, separator, reply = value.partition("|")
            identifier = identifier.strip().lstrip("#").upper()
            if separator and reply.strip() and identifier in self._offered_objection_ids:
                objection = next(item for item in self.user_objections if item["id"] == identifier)
                if not any(item["message_id"] == message_id for item in objection["replies"]):
                    objection["replies"].append({"speaker": speaker, "round": round_number,
                                                 "message_id": message_id, "text": reply.strip()})
            else:
                self.meta_warnings.append(f"{speaker}: kullanıcı itirazı yanıtı eşleştirilemedi: {value}")

    def evidence_report(self):
        """Consensus is computed from recorded evidence, never invented by the summary model."""
        assessments = self.decision_assessments()
        agreed = [a for a in assessments if a["consensus"]]
        lines = ["## Üzerinde uzlaşılan konular", ""]
        lines += [f"- {a['id']}: {a['text']} — son turda tüm katılımcılar açıkça destekledi." for a in agreed] or ["Açık destek kayıtlarıyla doğrulanmış ortak karar yok."]
        lines += ["", "## Görüş ayrılığı devam eden konular", ""]
        for item in assessments:
            if item["consensus"]:
                continue
            lines.append(f"- {item['id']}: {item['text']} — uzlaşı doğrulanmadı.")
            if item["missing_final_support"]:
                lines.append("  Son tur açık desteği bulunmayanlar: " + ", ".join(item["missing_final_support"]))
            for vote in item["opponents"]:
                lines.append(f"  Karşı görüş ({vote['speaker']}): {vote['reason']}")
            for objection in item["objections"]:
                lines.append(f"  Açık itiraz {objection['id']} ({objection['speaker']}): {objection['reason']}")
        lines += [f"- Açık konu {i['id']}: {i['baslik']}" for i in self.issues if i["durum"] == "AÇIK"]
        if not any(not a["consensus"] for a in assessments) and not any(i["durum"] == "AÇIK" for i in self.issues):
            lines.append("Kayıtlarda açık ayrılık yok; bu tek başına uzlaşı kanıtı değildir.")
        lines += ["", "## Fikrini değiştiren ajanlar ve gerekçeleri", ""]
        changes = [o for o in self.opinions if o["changed"]]
        lines += [f"- Tur {o['round']}, {o['speaker']} / {o['topic']}: {o['previous']} → {o['stance']}. Gerekçe: {o['reason']}" for o in changes] or ["Kayıtlı görüş değişikliği yok."]
        lines += ["", "## Cevaplanmamış sorular", ""]
        questions = [q for q in self.questions if q["status"] == "open"]
        lines += [f"- {q['id']}: {q['from']} → {q['to']}: {q['text']}" for q in questions] or ["Kayıtlı cevapsız soru yok."]
        if self.user_objections:
            lines += ["", build_user_objection_report(self.user_objections)]
        if self.unresolved_references or self.meta_warnings:
            lines += ["", "### Takip uyarıları", "", "META takibi eksik veya eşleşmeyen işlemler var; insan incelemesi gerekli."]
            lines += [f"- {r['speaker']}, {r['type']}: {r['value']}" for r in self.unresolved_references]
            lines += [f"- {warning}" for warning in self.meta_warnings]
        cancelled = [d for d in self.decisions if d["durum"] == "İPTAL"]
        if cancelled:
            lines += ["", "### İptal edilen öneriler", ""]
            lines += [f"- {d['id']}: {d['ozet']}" for d in cancelled]
        return "\n".join(lines)

    def _previous_round_context(self, round_number):
        lines = []
        for entry in self.transcript:
            if entry["round"] != round_number - 1:
                continue
            text = entry["text"]
            if len(text) > 3200:
                text = text[:1600] + "\n[Mesajın orta bölümü kısaltıldı]\n" + text[-1600:]
            reference = entry.get("user_objection_id") or entry.get("message_id")
            prefix = f"[{reference}] " if reference else ""
            lines.append(f"{prefix}{entry['speaker']}: {text}")
        return "\n\n".join(lines) or "Önceki tur kaydı yok."

    def _negotiation_context(self, name):
        latest = {}
        for opinion in self.opinions:
            latest[(opinion["speaker"], opinion["topic"], opinion.get("model_revision", 0))] = opinion
        return json.dumps({"katilimcilar": [p["name"] for p in self.personas],
                           "son_gorusler": list(latest.values()),
                           "sana_sorulan_acik_sorular": [q for q in self.questions if q["to"] == name and q["status"] == "open"],
                           "soru_ve_yanitlar": self.questions,
                           "acik_itirazlar": [o for o in self.objections if o["status"] == "open"],
                           "iptal_edilen_oneriler": [d for d in self.decisions if d["durum"] == "İPTAL"],
                           "kullanici_talimatlari": self.user_instructions,
                           "kullanici_itirazlari": self.user_objections,
                           "model_devirleri": self.model_handoffs,
                           "eslesmeyen_islemler": self.unresolved_references}, ensure_ascii=False)

    def _format_open_issues(self):
        open_issues = [i for i in self.issues if i["durum"] == "AÇIK"]
        if not open_issues:
            return "Şu an açık konu yok."
        return "\n".join(f"- (#{i['id']}, Tur {i['tur']}, {i['acan']}) {i['baslik']}" for i in open_issues)

    def _format_decisions(self):
        active_decisions = [d for d in self.decisions if d["durum"] == "AKTİF"]
        if not active_decisions:
            return "Henüz alınmış bir karar yok."
        return "\n".join(f"- (#{d['id']}, Tur {d['tur']}) {d['ozet']}" for d in active_decisions)

    def _actual_model(self, provider, requested):
        details = self._last_call_details
        if details.get("provider") == provider and details.get("requested_model") == requested:
            return details.get("actual_model") or requested
        return requested

    def _save_call_usage(self, provider, requested, actual, prompt_tokens=None, completion_tokens=None, cost=None, finish_reason=None):
        details = {"provider": provider, "requested_model": requested, "actual_model": actual or requested,
                   "original_requested_model": self._request_context.get("requested_model", requested),
                   "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                   "total_tokens": prompt_tokens + completion_tokens if prompt_tokens is not None and completion_tokens is not None else None,
                   "cost": cost, "finish_reason": finish_reason, "layer": self.routing_config["mode"],
                   "purpose": self._request_context.get("purpose", "")}
        with self._lock:
            self._last_call_details = details
            self.api_calls.append(details)

    # ------------------------------------------------------------------
    # Alt seviye: tek bir saglayiciya ham cagri
    # ------------------------------------------------------------------
    def _raw_call(self, prov, model, prompt, key, system_prompt=None):
        timeout = max(30, min(60, float(self.routing_config.get("response_timeout_seconds", 60))))
        try:
            result = self._request_runner(provider=prov, model=model or DEFAULT_MODELS[prov], prompt=prompt, key=key,
                system_prompt=system_prompt, mode=self.routing_config["mode"], timeout_seconds=timeout,
                cancel_event=self._stop_event, skip_event=self._skip_event)
        except api_transport.RequestCancelled:
            raise DiscussionStopped() from None
        with self._lock:
            self._check_stop()
            if self._skip_event.is_set():
                raise api_transport.ModelSkipped("Kullanıcı diğer modeli seçti")
            self._network_active = False
            self._save_call_usage(prov, model or DEFAULT_MODELS[prov], result.get("actual_model"),
                                  result.get("prompt_tokens"), result.get("completion_tokens"),
                                  result.get("cost"), result.get("finish_reason"))
        return result.get("text")

    def request_skip_current_model(self, expected_request_id=None):
        with self._lock:
            if not self._network_active or self._stop_event.is_set():
                return False
            if expected_request_id is not None and expected_request_id != self._current_request_id:
                return False
            self._skip_event.set()
            return True

    # ------------------------------------------------------------------
    # Orta seviye: gecikme + rate-limit'te otomatik yeniden deneme
    # ------------------------------------------------------------------
    def _route_event(self, kind, **details):
        event = {"time": datetime.now(timezone.utc).isoformat(), "event": kind,
                 "layer": self.routing_config["mode"], **self._request_context, **self._active_route, **details}
        with self._lock:
            self.routing_events.append(event)
            self.checkpoint()
        self.routing_event.emit(dict(event))
        return event

    def _routing_wait(self, seconds, reason="Bekleme"):
        self._check_stop()
        if seconds > 0:
            self.status.emit(f"{reason}: {seconds:.1f} sn. Durdur ile iptal edebilirsiniz.")
            self._route_event("wait", seconds=round(seconds, 2), reason=reason)
            if self._stop_event.wait(seconds):
                raise DiscussionStopped()

    def _call_provider_with_retry(self, prov, model, prompt, key, system_prompt=None, allow_retry=True):
        cfg = self.routing_config
        interval = max(4, float(cfg["free_interval_seconds"])) if cfg["mode"] == "free" else max(0, float(cfg["paid_interval_seconds"]))
        pace_key = self.pacer.key(prov, key)
        retries = max(0, min(MAX_RETRIES, int(cfg["max_retries"])))
        if not allow_retry or (cfg["mode"] == "free" and not cfg.get("free_retry_same_model", False)):
            retries = 0
        for attempt in range(retries + 1):
            self._active_route.update(provider=prov, model=model, attempt=attempt + 1, attempt_limit=retries + 1)
            self._check_stop()
            if self.pacer.daily_blocked(pace_key):
                raise DailyQuotaExhausted("Sağlayıcının ortak günlük kotası dolu; aynı hesapta model değişimi kotayı yenilemez.")
            self.pacer.acquire(pace_key, interval, lambda delay: self._routing_wait(delay, "Çağrı aralığı / ortak kota beklemesi"))
            self._check_stop()
            self.status.emit(f"{PROVIDER_LABELS.get(prov, prov)} / {model}: yanıt bekleniyor...")
            with self._lock:
                self._skip_event.clear()
                self._request_sequence += 1
                self._current_request_id = f"request-{self._request_sequence}"
                self._network_active = True
                self._active_route.update(request_id=self._current_request_id,
                    timeout_seconds=max(30, min(60, float(cfg.get("response_timeout_seconds", 60)))))
            try:
                self._route_event("attempt", provider=prov, model=model, attempt=attempt + 1)
                text = self._raw_call(prov, model, prompt, key, system_prompt=system_prompt)
                with self._lock:
                    self._network_active = False
                if not text or not text.strip():
                    raise EmptyResponseError("Görünür yanıt metni yok")
                if (system_prompt is not None or META_BLOCK_MARKER in text) and not self._parse_meta_block(text)[0]:
                    raise routing.MetaOnlyResponseError("Yanıt yalnızca yapılandırılmış kayıt içeriyor; görünür metin yok")
                self._route_event("success", provider=prov, model=self._actual_model(prov, model), attempt=attempt + 1)
                return text
            except DiscussionStopped:
                raise
            except Exception as exc:
                with self._lock:
                    self._network_active = False
                self._check_stop()
                kind = error_kind(exc)
                retry_after = retry_after_seconds(exc)
                item = {"provider": prov, "model": model, "error_type": getattr(exc, "remote_error_type", type(exc).__name__),
                        "http_status": getattr(exc, "status_code", None), "attempt": attempt + 1,
                        "reason": kind, "retry_after": retry_after, "limit_source": routing.limit_source(exc),
                        "response_detail": "meta_only" if isinstance(exc, routing.MetaOnlyResponseError) else
                                           "empty_content" if isinstance(exc, EmptyResponseError) else None}
                self.api_errors.append(item)
                self._route_event("failure", **item)
                if kind == "daily_quota":
                    self.pacer.block_daily(pace_key)
                    raise DailyQuotaExhausted("Ortak günlük kota dolu; ücretsiz model havuzunda tekrar çağrı yapılmadı.") from None
                if kind == "account_rate_limit":
                    self.pacer.cooldown(pace_key, max(retry_after, float(cfg["backoff_seconds"])))
                if kind in ("rate_limit", "upstream_rate_limit", "account_rate_limit", "temporary") and attempt < retries:
                    delay = max(retry_after, float(cfg["backoff_seconds"]) * (2 ** attempt))
                    if delay <= 120:
                        self._route_event("retry", provider=prov, model=model, seconds=delay)
                        self._routing_wait(delay, "Kota / geçici hata sonrası yeniden deneme")
                        continue
                if kind in ("rate_limit", "upstream_rate_limit", "response_timeout", "user_skip"):
                    self._model_cooldowns[(prov, model)] = time.monotonic() + max(retry_after, 60)
                raise
            finally:
                with self._lock:
                    self._network_active = False

    def _request_with_fallback(self, provider, model, prompt, system_prompt=None, purpose="discussion", role_name=None,
                               handoff=False):
        self._check_stop()
        pinned = self.role_models.get(role_name) if role_name is not None else None
        if pinned and not handoff:
            provider, model = pinned["provider"], pinned["route_model"]
        requested = {"requested_provider": provider, "requested_model": model, "purpose": purpose}
        self._request_context = requested
        self._active_route = {}
        if self.routing_config["mode"] == "free" and not self._quota_checked:
            self._quota_checked = True
            try:
                self.free_quota = self._quota_loader(self.api_keys.get("openrouter", ""))
            except Exception:
                self.status.emit("Ücretsiz kota sorgulanamadı; kalan kota bilinmiyor. API hataları ayrıca takip edilecek.")
            if self.free_quota:
                q = self.free_quota
                self.status.emit(f"OpenRouter günlük ücretsiz kota: kullanılan {q.get('used', '?')} / {q.get('limit', '?')}; kalan {q.get('remaining', '?')}.")
                self.checkpoint()
                if q.get("remaining") == 0:
                    self.pacer.block_daily(self.pacer.key("openrouter", self.api_keys.get("openrouter", "")))
                    raise DailyQuotaExhausted("OpenRouter günlük ücretsiz kota dolu; kalan 0. Yeni model isteği gönderilmedi. Kota yenilenince yeni oturum başlatabilirsiniz.")
        try:
            candidates = self.route_policy.candidates(provider, model, self.api_keys)
        except PolicyError as exc:
            if handoff:
                raise PinnedRoleUnavailable(f"Devir adayı {model} kullanılamıyor. {exc}") from None
            raise
        missing_primary = bool(candidates and (candidates[0]["provider"], candidates[0]["model"]) != (provider, model))
        history_skips = []
        track_role_candidates = role_name is not None and not pinned and self.routing_config.get("automatic_fallback")
        if track_role_candidates and self._failed_role_routes:
            for candidate in candidates:
                previous = self._failed_role_routes.get((candidate["provider"], candidate["model"]))
                if previous:
                    history_skips.append({**candidate, **previous})
            candidates = self.route_policy.candidates(provider, model, self.api_keys, excluded=self._failed_role_routes)
        if pinned:
            candidates = [c for c in candidates if (c["provider"], c["model"]) == (provider, model)]
            if handoff:
                self.status.emit(f"{role_name}: kullanıcı seçimiyle {model} devir için deneniyor.")
            else:
                self.status.emit(f"{role_name}: model sabit — {pinned['actual_model']}. Otomatik model değişimi kapalı.")
        event_start = len(self.routing_events)
        error_start = len(self.api_errors)
        self._route_event("plan", candidates=candidates,
                          automatic_fallback=not pinned and self.routing_config.get("automatic_fallback", True),
                          catalog_fallback=not pinned and self.routing_config.get("mode") == "free" and self.routing_config.get("free_catalog_fallback", True))
        if missing_primary and not pinned:
            self._route_event("skipped", provider=provider, model=model, reason="not_in_catalog")
        for previous in history_skips:
            self._route_event("skipped", reason="session_model_failed", **previous)
        attempts, blocked_providers = [], set()
        for index, candidate in enumerate(candidates):
            prov, mid = candidate["provider"], candidate["model"]
            self._active_route = {"provider": prov, "model": mid,
                                  "candidate_index": index + 1, "candidate_count": len(candidates)}
            self._check_stop()
            if prov in blocked_providers or self.pacer.daily_blocked(self.pacer.key(prov, self.api_keys[prov])):
                self._route_event("skipped", provider=prov, model=mid, reason="provider_blocked")
                continue
            cooldown = self._model_cooldowns.get((prov, mid), 0) - time.monotonic()
            if cooldown > 0:
                if pinned and self._retrying_round and cooldown <= 120:
                    self._routing_wait(cooldown, "Sabit modelin önceki hata sonrası bekleme süresi")
                else:
                    self._route_event("skipped", provider=prov, model=mid, reason="model_cooldown")
                    continue
            if (prov, mid) != (provider, model):
                self.status.emit(f"{model} yanıt veremedi; aynı katmanda {mid} deneniyor.")
                self._route_event("fallback", provider=prov, model=mid, reason="primary_unavailable")
            try:
                candidate_error_start = len(self.api_errors)
                if handoff:
                    text = self._call_provider_with_retry(prov, mid, prompt, self.api_keys[prov],
                                                          system_prompt=system_prompt, allow_retry=False)
                elif system_prompt is None:
                    text = self._call_provider_with_retry(prov, mid, prompt, self.api_keys[prov])
                else:
                    text = self._call_provider_with_retry(prov, mid, prompt, self.api_keys[prov], system_prompt=system_prompt)
                actual = self._actual_model(prov, mid)
                if handoff and (prov, actual) == (pinned["provider"], pinned["actual_model"]):
                    raise PinnedRoleUnavailable("Devir isteğini önceki model yanıtladı. Devir uygulanmadı.")
                if pinned and not handoff and actual != pinned["actual_model"]:
                    raise PolicyError("Sabit rolün gerçek model kimliği değişti; yanıt kabul edilmedi.")
                if role_name is not None and not pinned:
                    self.role_models[role_name] = {"provider": prov, "route_model": mid, "actual_model": actual}
                attempts.append({"provider": prov, "model": mid, "status": "success"})
                self._last_response_info = {**requested, "provider": prov, "model": actual,
                                            "fallback_used": (prov, mid) != (provider, model), "attempts": attempts}
                return text, prov, actual
            except (DiscussionStopped, PinnedRoleUnavailable):
                raise
            except PolicyError:
                raise
            except api_transport.TransportCleanupError:
                raise PolicyError("Önceki istek işlemi sonlandırılamadı; arka planda birikme olmaması için yeni model denenmedi.") from None
            except DailyQuotaExhausted:
                blocked_providers.add(prov)
                attempts.append({"provider": prov, "model": mid, "status": "daily_quota"})
            except Exception as exc:
                kind = error_kind(exc)
                attempts.append({"provider": prov, "model": mid, "status": kind})
                if track_role_candidates and kind in ROLE_ROUTE_FAILURES:
                    logged_error = self.api_errors[-1] if len(self.api_errors) > candidate_error_start else {}
                    self._failed_role_routes[(prov, mid)] = {
                        "previous_reason": kind,
                        "previous_purpose": purpose,
                        "http_status": getattr(exc, "status_code", None),
                        "response_detail": logged_error.get("response_detail"),
                    }
                if kind == "configuration" or (kind == "account_rate_limit" and retry_after_seconds(exc) > 120):
                    blocked_providers.add(prov)
        current_events = self.routing_events[event_start:]
        tried = list(dict.fromkeys(e["model"] for e in current_events if e.get("event") == "attempt"))
        skipped = sum(e.get("event") == "skipped" for e in current_events)
        reasons = {"empty_response": "görünür yanıt metni yok", "rate_limit": "kota/hız sınırı",
                   "upstream_rate_limit": "model sağlayıcısının ortak kapasite sınırı",
                   "account_rate_limit": "hesabın ortak hız sınırı", "daily_quota": "ortak günlük kota dolu",
                   "configuration": "anahtar veya hesap erişim sorunu", "temporary": "geçici servis hatası",
                   "model_unavailable": "model kullanılamıyor", "response_timeout": "toplam yanıt süresi doldu", "user_skip": "kullanıcı diğer modeli seçti"}
        errors = self.api_errors[error_start:]
        last = errors[-1] if errors else {}
        if not last:
            skipped_events = [e for e in current_events if e.get("event") == "skipped"]
            if skipped_events:
                latest = skipped_events[-1]
                last = {"model": latest.get("model", model), "reason": latest.get("reason")}
                if last["reason"] == "provider_blocked" and self.pacer.daily_blocked(self.pacer.key(provider, self.api_keys[provider])):
                    last["reason"] = "daily_quota"
        reasons.update(model_cooldown="önceki hata sonrası model geçici beklemede", provider_blocked="sağlayıcı erişimi kapalı",
                       session_model_failed="model bu oturumda daha önce başarısız oldu veya kullanıcı tarafından atlandı")
        cause = reasons.get(last.get("reason"), "uygun kullanılabilir model kalmadı")
        if last.get("http_status"):
            cause += f" (HTTP {last['http_status']})"
        detail = f"{len(tried)} model denendi, {skipped} aday atlandı. Son neden: {last.get('model', model)} — {cause}."
        if not self.routing_config.get("automatic_fallback"):
            detail += " Otomatik yedek seçimi ayarlarda kapalı."
        if handoff:
            raise PinnedRoleUnavailable(detail + f" {model} ile devir tamamlanamadı. Önceki model sabit kaldı.")
        if pinned:
            detail += f" {role_name} rolü {pinned['actual_model']} modeline sabit; başka modele geçilmedi."
            if last.get("reason") in ROLE_ROUTE_FAILURES | {"model_cooldown"}:
                raise PinnedRoleUnavailable(detail)
        raise RoutesExhausted(detail + " Katman dışına çıkılmadı; eksik oturum raporu kaydedildi.")

    # ------------------------------------------------------------------
    # Persona konuşma turu
    # ------------------------------------------------------------------
    def call_llm(self, persona, round_context, current_round, handoff=None):
        prov = persona["provider"]
        model = persona["model"]
        key = self.api_keys.get(prov, "")
        name = persona["name"]
        role = persona["role"]

        sys_prompt = (
            f"Sen planlama ve tartışma kurulunda görev yapan bir uzmansın.\n"
            f"Adın: {name}\n"
            f"Rolün ve Bakış Açın: {role}\n"
            f"Konu/Proje: {self.project}\n\n"
            f"Kurallar:\n"
            f"1. Kesinlikle rolüne sadık kal.\n"
            f"2. Açık konulara ve alınan kararlara göre yapıcı ve doğrudan katkı sun; "
            f"zaten çözülmüş bir konuyu tekrar açma.\n"
            f"3. Gereksiz dolgu kelimelerden kaçın, net ve somut önerilerde bulun.\n"
            f"4. Senden sonra konuşacak katılımcıların ne diyeceğini henüz bilmiyorsun; "
            f"sadece şu ana kadar konuşulanlara göre yorum yap.\n"
            f"5. Eğer bu cevabında, kendi açtığın veya başka birinin daha önce açtığı bir konuyu "
            f"somut ve yeterli şekilde yanıtladıysan, o konuyu AÇIK olarak tekrar işaretleme; "
            f"ÇÖZÜLDÜ olarak belirt.\n"
            f"6. Eğer kullanıcı veya bir katılımcı, daha önce alınmış bir kararı iptal ettiyse ya da "
            f"değiştirdiyse, o eski kararı asla tekrar önerme veya üzerine detay ekleme; bunun yerine "
            f"iptal edildiğini META bloğunda İPTAL alanıyla belirt.\n"
            f"7. Önce kullanıcıya gösterilecek görüşünü normal metinle yaz. "
            f"En az bir gerekçe veya somut öneri ver. Ardından son satırlarda şu takip bloğunu ekle "
            f"(başlıkları değiştirme, karşılığı yoksa 'yok' yaz):\n"
            f"{META_BLOCK_MARKER}\n"
            f"AÇIK: <yeni ya da hâlâ çözülmemiş bir konu, yoksa 'yok'>\n"
            f"ÇÖZÜLDÜ: <daha önce açık olan bir konuyu çözdüysen kısa başlığı, yoksa 'yok'>\n"
            f"KARAR: <somut önerinin kısa özeti; ortak karar anlamına gelmez, yoksa 'yok'>\n"
            f"İPTAL: <daha önce alınmış ama artık geçersiz olan bir kararın kısa özeti, yoksa 'yok'>"
        )

        sys_prompt += (
            "\nDiğer katılımcıların görüşlerine isim vererek doğrudan yanıt ver. Sana yöneltilen açık "
            "soruları yanıtla; eski açıklamanı tekrarlamak yerine yeni kanıt, yanıt veya itiraz ekle. "
            "Fikrin değiştiyse eski görüşünü ve gerekçeyi açıkla. KARAR bir öneridir, ortak karar değildir. "
            "İptalde D kimliğini, konu çözümünde I kimliğini aynen kullan. Kullanıcı talimatları önceliklidir. "
            "Her tur en az bir GÖRÜŞ satırı yaz. Her alanı birden fazla satırda kullanabilirsin; "
            "her satır bir kayıt. Olmayan alanları yazma. "
            "Ek META biçimleri (ayırıcı |):\n"
            "SORU: Katılımcı adı | soru\nYANIT: Q1 | somut cevap\n"
            "GÖRÜŞ: sabit konu başlığı | şu anki görüşün | gerekçe (değiştiyse neden)\n"
            "İTİRAZ: D1 | itiraz gerekçesi\nİTİRAZ_ÇÖZÜLDÜ: O1 | kendi itirazının çözülme gerekçesi\n"
            "DESTEK: D1 | gerekçe\nKARŞI: D1 | gerekçe\n"
            "Kullanıcı itirazına yanıt verirken U kimliğini aynen kullan. Görünür metinde neyi değiştirdiğini "
            "veya neden koruduğunu açıkla; ardından MÜDAHALE_YANITI: U1 | somut gerekçe yaz. "
            "İtirazı değerlendirmediysen yanıt kaydı oluşturma.\n"
            "Son turda mevcut öneriler için kendi destek/karşı görüşünü gerekçeyle belirt. "
            "Sessizlik onay değildir; öneren kişi de açık destek vermelidir. "
            "Başka bir katılımcının sorusunu onun adına yanıtlama veya itirazını kapatma."
        )
        with self._lock:
            self._drain_user_messages(round_context)
            open_issues_str = self._format_open_issues()
            decisions_str = self._format_decisions()
            common_record = (self._previous_round_snapshot[1]
                             if self._previous_round_snapshot and self._previous_round_snapshot[0] == current_round
                             else self._previous_round_context(current_round))
            round_str = "\n".join(round_context) or "Bu turda henüz yeni mesaj yok."
            negotiation = self._negotiation_context(name)
            self._offered_objection_ids = {item["id"] for item in self.user_objections}

        phase = ("İlk ve son değerlendirme" if self.rounds == 1 else
                 "Son değerlendirme; önerilere açık destek/karşı görüş bildir" if current_round == self.rounds else
                 "İlk görüşleri sun ve sorular yönelt" if current_round == 1 else
                 "Diğer görüşleri sorgula, soruları yanıtla ve itirazları değerlendir")
        prompt = (
            f"Tur {current_round}/{self.rounds}; görev: {phase}.\n"
            f"Açık konular:\n{open_issues_str}\n\n"
            f"Şu ana kadar kaydedilen öneriler (ortak karar değildir):\n{decisions_str}\n\n"
            f"Önceki turun ortak kaydı:\n{common_record}\n\n"
            f"Bu turda ({current_round}. tur) şu ana kadar konuşulanlar:\n{round_str}\n\n"
            f"Müzakere belleği:\n{negotiation}\n\n"
            f"Lütfen {current_round}. tur için görüşünü belirt."
        )

        if handoff:
            previous = self.role_models[name]["actual_model"]
            prompt += (f"\nKullanıcı bu turda {name} rolünü önceki model {previous} yerine sana devretmeyi seçti. "
                       "Ortak kayıttaki bu rolün önceki önerisini ve bu turdaki yeni yanıtları değerlendir. "
                       "Önceki öneriyi koruduğunu, değiştirdiğini veya reddettiğini ve nedenini açıkça belirt. "
                       "Önceki modelin cevabını kendi eski görüşün gibi anlatma.")
            prov, model = handoff["provider"], handoff["model"]
        options = {"handoff": True} if handoff else {}
        text, _, _ = self._request_with_fallback(prov, model or DEFAULT_MODELS[prov], prompt, sys_prompt,
                                                purpose=f"Tur {current_round} / {name}", role_name=name, **options)
        return text

    # ------------------------------------------------------------------
    # Özet: istenirse belirli sağlayıcı/model, başarısız olursa otomatik yedek zincir
    # Donen deger: (ozet_metni, kullanilan_saglayici, kullanilan_model)
    # Not: artik ham transkript degil, biriktirilmis issues/decisions
    # kullaniliyor -> hem daha tutarli (yorum degil, veri) hem daha ucuz.
    # ------------------------------------------------------------------
    def synthesize(self, provider=None, model=None):
        if self.completed_rounds != self.rounds:
            raise RuntimeError("Nihai rapor için seçilen tüm turlar tamamlanmalı.")
        evidence = self.evidence_report()
        prompt = (
            "İnsan denetimli bir karar destek raporunun yalnızca 'Nihai öneri ve uygulanabilir sonraki adımlar' "
            "bölümünü yaz. Aşağıdaki kanıt bölümleri yazılım tarafından hesaplandı; değiştirme, yeniden üretme. "
            "Öneriler ortak karar değildir. Açık destek yoksa uzlaşı var deme. Anlaşmazlıkları, cevapsız soruları "
            "ve iptalleri koru. Önerileri koşulları ve gerekçeleriyle sun; eksik bilgiyi uydurma. "
            "İptal edilen önerileri uygulanacak adım olarak sunma. Kullanıcı itirazına verilen yanıt insan "
            "incelemesi bekler. Model devrini eski modelin görüş değişikliği olarak yorumlama. "
            "Bu rapor insan onayı gerektirir.\n\n"
            f"Konu: {self.project}\n\n{evidence}\n\n"
            "Müzakere kayıtları: " + json.dumps({
                "gorusler": self.opinions, "sorular": self.questions, "itirazlar": self.objections,
                "oneriler": self.decisions, "destek_karsi": self.votes,
                "kullanici_talimatlari": self.user_instructions,
                 "model_devirleri": self.model_handoffs,
            }, ensure_ascii=False)
        )

        prov = provider or self.summary_provider or "openrouter"
        resolved_model = model or self.summary_model or DEFAULT_MODELS[prov]
        try:
            text, used_prov, used_model = self._request_with_fallback(prov, resolved_model, prompt, purpose="summary")
        except DiscussionStopped:
            raise
        except (PolicyError, RoutesExhausted, DailyQuotaExhausted) as exc:
            raise SummaryFailed("Özet oluşturulamadı. " + str(exc)) from None
        self._check_stop()
        if not text or not text.strip():
            raise SummaryFailed("Özet modeli boş yanıt döndürdü.")
        consensus_ids = [a["id"] for a in self.decision_assessments() if a["consensus"]]
        verification = ("Kayıtlardan doğrulanmış uzlaşı: " + ", ".join(consensus_ids) + ".") if consensus_ids else "Kayıtlardan doğrulanmış uzlaşı YOK."
        notice = (f"> **Sistem doğrulaması:** {verification} Aşağıdaki bölüm model önerisidir; "
                  "doğrulanmış karar değildir. Çelişki halinde yukarıdaki kayıtlar esas alınmalıdır.\n\n"
                  "### Modelin önerisi — insan incelemesi gerekli\n\n")
        return evidence + "\n\n## Nihai öneri ve uygulanabilir sonraki adımlar\n\n" + notice + text, used_prov, used_model


class SummaryWorker(QThread):
    result = pyqtSignal(str, str)
    error = pyqtSignal(str)

    def __init__(self, discussion, provider=None, model=None):
        super().__init__()
        self.discussion = discussion
        self.provider, self.model = provider, model

    def run(self):
        try:
            text, source = self.discussion.generate_summary_only(self.provider, self.model)
            self.result.emit(text, source)
        except SummaryFailed as exc:
            self.error.emit(str(exc) + " Önceki rapor korundu.")
        except Exception as exc:
            self.error.emit(f"Özet oluşturulamadı ({type(exc).__name__}). Önceki rapor korundu.")
