import json
import os
import tempfile
from html import escape
from uuid import uuid4
from datetime import datetime

from .config import SESSIONS_DIR


def new_session_dir(project_text):
    safe_title = "".join(c if c.isalnum() else "_" for c in project_text[:30]).strip("_")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dir_name = f"{timestamp}_{safe_title}_{uuid4().hex[:8]}"
    session_path = SESSIONS_DIR / dir_name
    session_path.mkdir(parents=True, exist_ok=True)
    return session_path


def atomic_write(path, content):
    """Keep the last valid file if writing the replacement fails."""
    fd, temporary = tempfile.mkstemp(prefix=".boardroom-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_transcript_json(session_path, project, transcript, **state):
    data = {
        "project": project,
        "timestamp": datetime.now().isoformat(),
        "transcript": transcript
    }
    data.update(state)
    atomic_write(session_path / "transcript.json", json.dumps(data, ensure_ascii=False, indent=4))


def build_user_objection_report(objections):
    lines = ["### Kullanıcı itirazları", ""]
    for objection in objections:
        status = "Yanıt kaydedildi · insan incelemesi gerekli" if objection["replies"] else "Yanıt bekliyor"
        lines.append(f"- {objection['id']} ({objection['message_id']}, Tur {objection['round']}): "
                     f"{objection['text']} — **{status}**")
        for reply in objection["replies"]:
            lines.append(f"  - {reply['message_id']}, Tur {reply['round']}, {reply['speaker']}: {reply['text']}")
    return "\n".join(lines)


def build_summary_report(project, transcript, summary_text, summary_source_label="Bilinmiyor", session_status="completed", state=None):
    """Ozet (kim cikardi) + tur tur tartisma dokumu tek bir Markdown metninde."""
    lines = [
        "# Özet ve Plan" if session_status == "completed" else "# Oturum Kaydı — Eksik / İşlem Sürüyor",
        "",
        f"**Konu:** {project}",
        f"**Ozeti cikaran:** {summary_source_label}",
        f"**Oturum durumu:** {session_status}",
        "**İnsan incelemesi gereklidir.**",
        f"**Tarih:** {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "---",
        "",
        summary_text,
        "",
        "---",
        "",
    ]
    if state:
        handoffs = state.get("model_handoffs", [])
        if handoffs:
            lines.extend(["## Kullanıcı seçimiyle model devirleri", ""])
            for item in handoffs:
                old, new = item["from"], item["to"]
                lines.append(f"- Tur {item['round']} · {item['speaker']} · "
                             f"{old['provider']} / {old['actual_model']} → {new['provider']} / {new['actual_model']} "
                             f"({item['message_id']}).")
            lines.append("")
        failures = state.get("round_failures", [])
        if failures:
            lines.extend(["## Turda eksik kalan yanıtlar ve yeniden denemeler", ""])
            for failure in failures:
                outcome = (f"Yanıt: {failure['recovered_by']}" if failure["recovered_by"] else "Yanıt eksik")
                target = f" · Devir adayı: {failure['handoff_target']}" if failure.get("handoff_target") else ""
                lines.append(f"- Tur {failure['round']} · {failure['speaker']} / {failure['model']} · "
                             f"{outcome}{target}. {failure['reason']}")
            lines.append("")
        if session_status != "completed" and state.get("user_objections"):
            lines.extend([build_user_objection_report(state["user_objections"]), "", "---", ""])
        lines.extend([build_routing_report(state), "", "---", ""])
    lines.extend(["## Tur Tur Tartisma", ""])

    grouped = {}
    order = []
    for entry in transcript:
        key = entry.get("round") or 0
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(entry)

    for key in sorted(order, key=lambda k: (k == 0, k)):
        title = "Ek Notlar" if key == 0 else f"Tur {key}"
        lines.append(f"### {title}")
        lines.append("")
        for entry in grouped[key]:
            model_txt = f" — {entry['model']}" if entry.get("model") else ""
            references = " · ".join(entry[key] for key in ("message_id", "user_objection_id") if entry.get(key))
            reference_text = f" [{references}]" if references else ""
            lines.append(f"**{entry['speaker']}{model_txt}{reference_text}:**")
            lines.append("")
            if entry.get("provider"):
                lines.append(f"API: {entry['provider']} | Yanıtlayan model: {entry.get('model', 'bilinmiyor')}")
                if entry.get("requested_model"):
                    lines.append(f"İstenen: {entry.get('requested_provider', entry['provider'])} / {entry['requested_model']}")
                if entry.get("fallback_used"):
                    lines.append("**Yedek model kullanıldı; ajan rolü korundu.**")
                if entry.get("model_handoff"):
                    item = entry["model_handoff"]
                    lines.append(f"**Kullanıcı seçimiyle model devri: {item['from']['actual_model']} → "
                                 f"{item['to']['actual_model']}.**")
                lines.append("")
            lines.append(entry["text"])
            lines.append("")

    return "\n".join(lines)


def save_summary_md(session_path, project, transcript, summary, summary_source_label="Bilinmiyor", state=None):
    content = build_summary_report(project, transcript, summary, summary_source_label, state=state)
    atomic_write(session_path / "ozet.md", content)


def save_incomplete_report(session_path, project, transcript, state):
    text = "Nihai karar raporu oluşturulmadı. Bu dosya tamamlanan konuşmaları ve deneme geçmişini koruyan eksik oturum raporudur."
    if state.get("last_error"):
        text += "\n\n" + state["last_error"]
    content = build_summary_report(project, transcript, text, "Nihai özet yok",
                                  session_status=state.get("status", "failed"), state=state)
    atomic_write(session_path / "oturum_raporu.md", content)


def build_routing_report(state):
    calls = state.get("api_calls", [])
    events = state.get("routing_events", [])
    mode = {"free": "Ücretsiz", "paid": "Ücretli / diğer", "legacy": "Eski kayıt"}.get(state.get("usage_mode", "legacy"), "Bilinmiyor")
    known = [c["cost"] for c in calls if isinstance(c.get("cost"), (int, float))]
    unknown = len(calls) - len(known)
    lines = ["## API kullanımı ve model değişimleri", "", f"Kullanım katmanı: {mode}",
             f"Uygulama denemesi: {sum(e.get('event') == 'attempt' for e in events) if events else 'eski kayıtta bilinmiyor'} | Yanıt kaydı: {len(calls)} | Hata kaydı: {len(state.get('api_errors', []))}",
             f"Bilinen maliyet toplamı: ${sum(known):.8f} | Maliyeti bildirilmeyen çağrı: {unknown}",
             "Bilinmeyen maliyet sıfır kabul edilmez. Kullanım bilgisi dönmeyen hatalı/yanıtsız denemelerin maliyeti doğrulanamaz. Uygulama sayacı sağlayıcının günlük kota sayacı değildir.", ""]
    quota = state.get("free_quota")
    if quota:
        lines.append(f"Başlangıç günlük kota sorgusu: {quota.get('used', '?')}/{quota.get('limit', '?')}; kalan {quota.get('remaining', '?')} (sorgu anındaki değer).")
    for role, identity in state.get("role_models", {}).items():
        lines.append(f"- Sabit rol: {role} → {identity['provider']} / {identity['actual_model']}")
    for call in calls:
        original = call.get("original_requested_model", call.get("requested_model", "?"))
        lines.append(f"- İstenen: {original} → Yanıtlayan: {call.get('provider', '?')} / {call.get('actual_model', '?')}"
                     f" | Giriş/çıkış token: {call.get('prompt_tokens', '?')}/{call.get('completion_tokens', '?')}"
                     f" | Maliyet: {call.get('cost') if call.get('cost') is not None else 'bildirilmedi'}")
    labels = {"plan": "Model sırası", "failure": "Hata", "retry": "Yeniden deneme", "fallback": "Yedek modele geçiş",
              "wait": "Bekleme", "skipped": "Atlandı", "attempt": "İstek", "success": "Yanıt alındı"}
    for event in events:
        if event.get("event") == "plan":
            lines.append("- Bu adımın model sırası: " + " → ".join(item["model"] for item in event.get("candidates", [])))
        lines.append(f"- {event.get('time', '')} | {labels.get(event.get('event'), event.get('event', '?'))}"
                     f" | {event.get('purpose', '')} | {event.get('provider', '')} / {event.get('model', '')}"
                     f" | HTTP: {event.get('http_status', '—')} | Neden: {event.get('reason', '—')}"
                     + (f" | Önceki deneme: {event['previous_purpose']} / {event['previous_reason']}"
                        if event.get("previous_reason") else "")
                     + f" | Sınır kaynağı: {event.get('limit_source') or '—'}"
                     f" | Yanıt ayrıntısı: {event.get('response_detail') or '—'} | Bekleme: {event.get('seconds', '—')} sn")
    return "\n".join(lines)


def build_api_html(state):
    from html import escape
    def safe(value):
        return escape(str(value))
    calls = state.get("api_calls", [])
    known = [c["cost"] for c in calls if isinstance(c.get("cost"), (float, int))]
    mode = {"free": "Ücretsiz", "paid": "Ücretli / diğer"}.get(state.get("usage_mode"), "Eski kayıt")
    header = (f"<h2>API Kullanımı</h2><p>Katman: {safe(mode)} | "
              f"Yanıt kaydı: {len(calls)} | Bilinen maliyet: ${sum(known):.8f} | "
              f"Maliyeti bildirilmeyen: {len(calls) - len(known)}</p>"
              f"<p>Uygulama denemesi: {sum(e.get('event') == 'attempt' for e in state.get('routing_events', []))} | Hata kaydı: {len(state.get('api_errors', []))}</p>"
              "<p>Bilinmeyen maliyet sıfır sayılmaz. Kullanım bilgisi dönmeyen hatalı/yanıtsız denemelerin maliyeti doğrulanamaz. Maliyetler API'nin bildirdiği değerlerdir; bu sayaç sağlayıcının günlük kotası değildir.</p>")
    quota = state.get("free_quota")
    if quota:
        header += f"<p>Başlangıç günlük kota sorgusu: {safe(quota.get('used', '?'))}/{safe(quota.get('limit', '?'))}; kalan {safe(quota.get('remaining', '?'))}. Bu değer sorgu anına aittir.</p>"
    for role, identity in state.get("role_models", {}).items():
        header += f"<p>Sabit rol: {safe(role)} → {safe(identity['provider'])} / {safe(identity['actual_model'])}</p>"
    header += '<table border="1" cellpadding="5"><tr><th>İstenen</th><th>Yanıtlayan</th><th>API</th><th>Giriş/çıkış token</th><th>Maliyet</th></tr>'
    for call in calls:
        header += "<tr>" + "".join(f"<td>{safe(value)}</td>" for value in (
            call.get("original_requested_model", call.get("requested_model", "?")), call.get("actual_model", "?"),
            call.get("provider", "?"), f"{call.get('prompt_tokens', '?')} / {call.get('completion_tokens', '?')}",
            call.get("cost") if call.get("cost") is not None else "Bildirilmiyor")) + "</tr>"
    header += "</table><h3>Bekleme, hata ve geçiş günlüğü</h3>"
    labels = {"plan": "Model sırası", "attempt": "İstek", "success": "Yanıt alındı", "failure": "Hata", "retry": "Yeniden deneme",
              "fallback": "Yedek modele geçiş", "wait": "Bekleme", "skipped": "Atlandı"}
    reasons = {"rate_limit": "Hız/kota sınırı", "daily_quota": "Günlük ortak kota dolu",
               "upstream_rate_limit": "Model sağlayıcısının ortak kapasite sınırı",
               "account_rate_limit": "Hesabın ortak hız sınırı", "primary_unavailable": "İlk model yanıt veremedi",
               "model_cooldown": "Model geçici beklemede", "provider_blocked": "Sağlayıcı kotası/erişimi kapalı",
               "session_model_failed": "Önceki rolde başarısız oldu veya atlandı. Yeni istek gönderilmedi",
               "configuration": "Anahtar veya hesap erişim sorunu", "temporary": "Geçici servis hatası",
               "model_unavailable": "Model kullanılamıyor", "empty_response": "Görünür yanıt metni yok"}
    reasons.update(response_timeout="Toplam yanıt süresi doldu", user_skip="Kullanıcı diğer modeli seçti")
    for event in state.get("routing_events", []):
        detail = "Yalnızca META geldi" if event.get("response_detail") == "meta_only" else "Boş içerik" if event.get("response_detail") == "empty_content" else ""
        if event.get("previous_reason"):
            previous = reasons.get(event["previous_reason"], event["previous_reason"])
            detail = " · ".join(part for part in (detail, f"Önceki deneme: {event['previous_purpose']} / {previous}") if part)
        if event.get("event") == "plan":
            header += "<p><b>Bu adımın model sırası:</b> " + safe(" → ".join(item["model"] for item in event.get("candidates", []))) + "</p>"
        header += "<p>" + safe(f"{event.get('time', '')} | {labels.get(event.get('event'), event.get('event'))} | {event.get('purpose', '')} | "
                              f"{event.get('provider', '')} / {event.get('model', '')} | {reasons.get(event.get('reason'), event.get('reason', ''))} | "
                              f"HTTP {event.get('http_status', '—')} | {detail} | {event.get('seconds', '—')} sn") + "</p>"
    return header


def load_session_report(session_path):
    """Prefer the authoritative checkpoint; preserve legacy Markdown-only sessions."""
    json_path = session_path / "transcript.json"
    md_path = session_path / "ozet.md"
    data = {}
    if json_path.exists():
        data = json.loads(json_path.read_text(encoding="utf-8"))
    if "status" in data:
        report = build_summary_report(data.get("project", ""), data.get("transcript", []),
            data.get("summary") or "Nihai rapor oluşturulmadı.",
            data.get("summary_source") or "—", session_status=data["status"], state=data)
    elif md_path.exists():
        report = md_path.read_text(encoding="utf-8")
    elif data:
        report = build_summary_report(data.get("project", ""), data.get("transcript", []),
                                      "Eski kayıt; nihai durum bilgisi yok.", session_status="legacy")
    else:
        report = "Kayıt bulunamadı."
    return report, data


def build_tracking_html(state):
    """Readable current and historical evidence without exposing raw JSON."""
    def safe(value):
        return escape(str(value))

    def table(headings, rows):
        header = "".join(f"<th>{safe(h)}</th>" for h in headings)
        body = "".join("<tr>" + "".join(f"<td>{safe(c)}</td>" for c in row) + "</tr>" for row in rows)
        return f'<table border="1" cellpadding="6" cellspacing="0"><tr>{header}</tr>{body}</table>' if body else "<p>Kayıt yok.</p>"

    labels = {"running": "Tartışılıyor", "paused": "Tur sonunda bekliyor", "stopped": "Durduruldu / eksik",
              "round_incomplete": "Tur eksik · sabit model yeniden denenebilir",
              "failed": "Hata / eksik", "summary_failed": "Turlar tamam; rapor başarısız",
              "summarizing": "Nihai rapor hazırlanıyor", "completed": "Rapor hazır — insan incelemesi gerekli",
              "review_required": "Son müdahale değerlendirilmedi / eksik", "save_failed": "Kayıt hatası"}
    status = state.get("status", "legacy")
    result = f"<h2>Müzakere takibi</h2><p>{safe(labels.get(status, status))} — Tamamlanan tur: {safe(state.get('completed_rounds', '?'))}/{safe(state.get('rounds', '?'))}</p>"
    if state.get("round_failures"):
        result += "<h3>Eksik yanıtlar ve yeniden denemeler</h3>" + table(
            ["Tur", "Katılımcı", "Sabit model", "Yanıt", "Neden"],
            [(f["round"], f["speaker"], f["model"], f["recovered_by"] or "Yanıt eksik", f["reason"])
             for f in state["round_failures"]])
    if state.get("model_handoffs"):
        result += "<h3>Kullanıcı seçimiyle model devirleri</h3>" + table(
            ["Tur", "Rol", "Önceki model", "Yeni model", "Devralma yanıtı"],
            [(item["round"], item["speaker"], item["from"]["actual_model"], item["to"]["actual_model"], item["message_id"])
             for item in state["model_handoffs"]])
    result += "<h3>Tur bazında görüşler</h3>" + table(
        ["Tur", "Katılımcı", "Model", "Konu", "Görüş", "Önceki görüş", "Değişiklik", "Gerekçe"],
        [(o["round"], o["speaker"], o.get("model") or "—", o["topic"], o["stance"], o["previous"],
          "Değişti" if o["changed"] else "—", o["reason"])
         for o in state.get("opinions", [])])
    assessments = {a["id"]: a for a in state.get("assessments", [])}
    result += "<h3>Öneriler ve ortak destek</h3>" + table(
        ["Kimlik", "Öneri", "Durum", "Ortak destek"],
        [(d["id"], d["ozet"], d["durum"],
          "İptal" if d["durum"] == "İPTAL" else
          "Son turda tüm katılımcılar destekledi" if assessments.get(d["id"], {}).get("consensus") else
          "Uzlaşı doğrulanmadı") for d in state.get("decisions", [])])
    result += "<h3>Sorular ve yanıtlar</h3>" + table(
        ["Kimlik", "Soran", "Muhatap", "Soru", "Durum", "Yanıt"],
        [(q["id"], q["from"], q["to"], q["text"], "Yanıtlandı" if q["status"] == "answered" else "Açık", q["answer"])
         for q in state.get("questions", [])])
    result += "<h3>İtirazlar</h3>" + table(
        ["Kimlik", "Öneri", "Katılımcı", "Gerekçe", "Durum", "Çözüm"],
        [(o["id"], o["decision_id"], o["speaker"], o["reason"],
          "Açık" if o["status"] == "open" else "Çözüldü", o.get("resolution", "")) for o in state.get("objections", [])])
    result += "<h3>Kullanıcı itirazları</h3>" + table(
        ["Kimlik", "Tur", "İtiraz", "Durum", "Yanıtlar"],
        [(o["id"], o["round"], o["text"],
          "İnsan incelemesi gerekli" if o["replies"] else "Yanıt bekliyor",
          " / ".join(f"{r['message_id']} · Tur {r['round']} · {r['speaker']}: {r['text']}" for r in o["replies"])
          or "—") for o in state.get("user_objections", [])])
    result += "<h3>Destek ve karşı görüş geçmişi</h3>" + table(
        ["Tur", "Öneri", "Katılımcı", "Model", "Tutum", "Gerekçe"],
        [(v["round"], v["decision_id"], v["speaker"], v.get("model") or "—",
          "Destek" if v["stance"] == "support" else "Karşı", v["reason"])
         for v in state.get("votes", [])])
    warnings = list(state.get("meta_warnings", []))
    warnings += [f"{r['speaker']} / {r['type']}: {r['value']} — eşleşmedi" for r in state.get("unresolved_references", [])]
    result += "<h3>Takip uyarıları</h3>" + ("<ul>" + "".join(f"<li>{safe(w)}</li>" for w in warnings) + "</ul>" if warnings else "<p>Uyarı yok.</p>")
    return result
