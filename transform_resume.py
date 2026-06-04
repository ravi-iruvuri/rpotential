"""
transform_resume.py — Transform a candidate's resume into a client template format.

Two modes:
  1. From a raw resume file (PDF/DOCX) — extracts structured JSON via Claude,
     stores in candidate_resumes, then renders.
  2. From an existing resume_id — skips extraction, renders directly.

Usage:
    # Full flow: parse candidate resume + render into template
    ./myenv/bin/python transform_resume.py \
        --candidate-id 42 \
        --resume-file "john_doe_resume.pdf" \
        --template-id 1 \
        --output john_doe_google.docx \
        --dsn 'postgresql://...'

    # Re-render from already-parsed resume
    ./myenv/bin/python transform_resume.py \
        --candidate-id 42 \
        --resume-id 7 \
        --template-id 1 \
        --output john_doe_google.docx \
        --dsn 'postgresql://...'

    # Dry-run: print the mapped JSON without writing files or DB rows
    ./myenv/bin/python transform_resume.py \
        --candidate-id 42 \
        --resume-file "john_doe_resume.pdf" \
        --template-id 1 \
        --dry-run \
        --dsn 'postgresql://...'
"""

import argparse
import json
import os
import sys
from io import BytesIO
from pathlib import Path

import anthropic
import psycopg
from jinja2 import Environment

# ── Step 1: extract structured JSON from candidate resume ────────────────────

EXTRACT_PROMPT = """Extract all information from this resume into JSON matching EXACTLY this schema:

{
  "candidate_name": "full name",
  "summary": "paragraph text or empty string",
  "education": [
    {"degree": "...", "school": "...", "city": "...", "state": "...", "year": "..."}
  ],
  "skills": ["skill1", "skill2"],
  "certifications": [
    {"cert_name": "...", "provider": "..."}
  ],
  "work_experience": [
    {
      "company": "...",
      "city": "...",
      "state": "...",
      "job_title": "...",
      "start_date": "...",
      "end_date": "...",
      "bullets": ["..."]
    }
  ]
}

Rules:
- If a section is missing from the resume, use an empty list [] or empty string ""
- Preserve exact bullet text from the resume
- For certifications without a named provider, use "Unknown"
- Return ONLY valid JSON, no markdown fences, no explanation.

Resume text:
"""


_SKILL_CONSOLIDATION_PROMPT = """\
You are preparing a candidate's skills list for a professional resume. Consolidate the raw \
skills below into AT MOST {max_lines} clean bullet-point entries.

RULES — apply strictly in this order:

1. VERSION MERGE — collapse version variants of the SAME technology:
   "Java 8", "Java 10", "Java 17"  →  "Java 8/10/17"
   "Python 2", "Python 3"          →  "Python 2/3"

2. DEDUPLICATE — keep only the most specific form when two names mean the same thing:
   "JavaScript", "JavaScript (ES6)"  →  "JavaScript"
   "ReactJS", "React"                →  "ReactJS"

3. GROUP ONLY WITHIN STRICT DOMAINS — only group skills that serve the EXACT same purpose:
   LANGUAGES:    "Java 8/10", "Python", "JavaScript", "C++"
   FRONTEND:     "ReactJS", "HTML5", "CSS3", "Sass", "Bootstrap4", "Figma"
   BACKEND:      "Spring Boot", "Hibernate", "JDBC", "JPA"
   API/SERVICES: "RESTful Web Services", "Microservices", "Designing APIs", "Consuming APIs"
                 →  "Microservices & RESTful Web Services (API Design/Consumption)"
   TESTING:      "JUnit4", "Mockito"  →  "Unit Testing (JUnit4/Mockito)"
   API TOOLS:    "Postman"  →  keep as "Postman" (it is a tool, not a testing framework)
   SOFT SKILLS:  "communication", "client interaction", "stakeholder management", "consultation"
                 →  "Communication & Stakeholder Management"

4. DO NOT GROUP across different domains:
   - DO NOT mix testing frameworks (JUnit4) with API client tools (Postman)
   - DO NOT mix design tools (Figma) with testing or backend tools
   - DO NOT merge "Design Patterns", "SOLID Principles", "Software Engineering" together —
     these are independent skills; list each separately or pair only Design Patterns + SOLID
     Principles if space is tight (they are complementary architecture principles)
   - DO NOT merge domain skills like "Machine Learning" or "Software Engineering" with
     languages or frameworks

5. PRIORITY when you must drop to hit the limit:
   Keep technical skills first; drop soft skills last.

6. Each output entry must be concise and professional (no trailing punctuation).

Raw skills:
{skills}

Return ONLY a JSON array of strings (max {max_lines} items), no markdown, no explanation:
["entry1", "entry2", ...]
"""


def consolidate_skills(skills: list, max_lines: int = 10, api_key: str = None) -> list:
    """Use Claude to merge, deduplicate and group skills into at most max_lines entries."""
    if not skills:
        return skills
    if len(skills) <= max_lines:
        return skills

    client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
    skills_text = "\n".join(f"- {s}" for s in skills)
    prompt = _SKILL_CONSOLIDATION_PROMPT.format(skills=skills_text, max_lines=max_lines)
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    consolidated = json.loads(raw)
    return consolidated[:max_lines]


def extract_resume_json(text: str) -> dict:
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": EXTRACT_PROMPT + text}],
    )
    raw = msg.content[0].text.strip()
    return json.loads(raw)


def extract_text_from_file(file_bytes: bytes, file_type: str) -> str:
    if file_type == "pdf":
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif file_type == "docx":
        from docx import Document
        doc = Document(BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        return file_bytes.decode("utf-8", errors="replace")


# ── Step 1b: normalize external JSON formats to canonical schema ─────────────

def _parse_iso_month(value: str) -> str:
    """Convert '2019-08' → 'August 2019'. Pass through anything else unchanged."""
    if not value or value.lower() == "present":
        return value or ""
    try:
        from datetime import datetime
        return datetime.strptime(value, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return value


def _split_location(location: str) -> tuple[str, str]:
    """'Indianapolis, IN' → ('Indianapolis', 'IN'). Handles missing state gracefully."""
    if not location:
        return "", ""
    parts = [p.strip() for p in location.split(",")]
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return parts[0], ""


def normalize_json(raw: dict | list) -> dict:
    """
    Convert an external extraction format (e.g. from Landing AI / Llama extractor)
    into our canonical schema:
      candidate_name, summary, education[], skills[], certifications[], work_experience[]

    Auto-detects the format by checking for known structural keys.
    Returns the input unchanged if it already looks like our canonical schema.
    """
    # Unwrap array wrapper  [{ "extraction": {...} }]
    if isinstance(raw, list):
        raw = raw[0] if raw else {}

    # Already canonical — has our expected top-level keys
    if "candidate_name" in raw:
        return raw

    # Landing AI / Llama extractor format — has "extraction" wrapper
    ext = raw.get("extraction", raw)

    candidate = ext.get("candidate", {})
    full_name = candidate.get("full_name") or ""

    # ── skills: merge technical + soft + domain into flat list ──────────────
    skills_block = ext.get("skills", {})
    if isinstance(skills_block, dict):
        skills = (
            skills_block.get("technical", []) +
            skills_block.get("soft", []) +
            skills_block.get("domain", [])
        )
    else:
        skills = skills_block or []
    skills = [s.get("value", s) if isinstance(s, dict) else s for s in skills]

    # ── education ────────────────────────────────────────────────────────────
    education = []
    for edu in ext.get("education", []):
        institution = edu.get("institution") or ""
        if isinstance(institution, dict):
            institution = institution.get("value", "")
        degree = edu.get("degree") or ""
        if isinstance(degree, dict):
            degree = degree.get("value", "")
        field = edu.get("field") or ""
        if isinstance(field, dict):
            field = field.get("value", "")
        end_date = edu.get("end_date") or ""
        if isinstance(end_date, dict):
            end_date = end_date.get("value", "")
        location = edu.get("location") or ""
        if isinstance(location, dict):
            location = location.get("value", "") or ""
        city, state = _split_location(location)
        year = (end_date or "")[:4]  # "2019-05" → "2019"
        degree_label = f"{degree} in {field}" if field else degree
        education.append({
            "degree": degree_label,
            "school": institution,
            "city": city,
            "state": state,
            "year": year,
        })

    # ── certifications ───────────────────────────────────────────────────────
    certifications = []
    for cert in ext.get("certifications", []):
        name = cert.get("name") or ""
        if isinstance(name, dict):
            name = name.get("value", "")
        issuer = cert.get("issuer") or ""
        if isinstance(issuer, dict):
            issuer = issuer.get("value", "")
        certifications.append({"cert_name": name, "provider": issuer or "Unknown"})

    # ── work experience ──────────────────────────────────────────────────────
    work_experience = []
    for job in ext.get("work_experience", []):
        company = job.get("company") or ""
        if isinstance(company, dict):
            company = company.get("value", "")
        title = job.get("title") or ""
        if isinstance(title, dict):
            title = title.get("value", "")
        location = job.get("location") or ""
        if isinstance(location, dict):
            location = location.get("value", "") or ""
        city, state = _split_location(location)
        start = job.get("start_date") or ""
        if isinstance(start, dict):
            start = start.get("value", "")
        end = job.get("end_date") or ""
        if isinstance(end, dict):
            end = end.get("value", "")
        responsibilities = job.get("responsibilities", [])
        bullets = [
            r.get("value", r) if isinstance(r, dict) else r
            for r in responsibilities
        ]
        work_experience.append({
            "company": company,
            "city": city,
            "state": state,
            "job_title": title,
            "start_date": _parse_iso_month(start),
            "end_date": _parse_iso_month(end),
            "bullets": bullets,
        })

    return {
        "candidate_name": full_name,
        "summary": ext.get("summary") or "",
        "education": education,
        "skills": skills,
        "certifications": certifications,
        "work_experience": work_experience,
    }


# ── Step 2: build HTML template from field_schema ────────────────────────────

def _jinja_var(expr: str) -> str:
    return "{{ " + expr + " }}"


def _jinja_tag(expr: str) -> str:
    return "{% " + expr + " %}"


def _item_fmt_to_jinja(item_fmt: str) -> str:
    """'{cert_name} - by {provider}' → '{{ item.cert_name }} - by {{ item.provider }}'"""
    import re
    result = item_fmt
    for ph in re.findall(r"\{(\w+)\}", item_fmt):
        result = result.replace("{" + ph + "}", _jinja_var("item." + ph))
    return result


def _section_header_html(label: str, bold: bool) -> str:
    weight = "bold" if bold else "normal"
    return f'<h2 style="font-weight:{weight}">{label}</h2>\n'


def build_html_template(schema: dict) -> str:
    font = schema.get("font_family", "Times New Roman")
    parts = []

    # ── CSS ───────────────────────────────────────────────────────────────────
    parts.append(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  @page {{ margin: 1in; }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: "{font}", "Times New Roman", Times, serif;
    font-size: 12pt;
    line-height: 1.2;
    color: #000;
  }}
  .name {{ text-align: center; font-weight: bold; margin-bottom: 14pt; }}
  h2 {{ font-size: 12pt; margin: 12pt 0 3pt 0; }}
  p {{ margin-bottom: 6pt; }}
  .body-text {{ text-align: justify; }}
  ul {{ margin: 2pt 0 8pt 24pt; padding: 0; }}
  li {{ text-align: justify; margin-bottom: 2pt; }}
  .exp-company {{ font-weight: bold; margin-top: 8pt; }}
  .exp-title   {{ font-weight: bold; }}
  .exp-dates   {{ font-weight: bold; margin-bottom: 3pt; }}
</style>
</head>
<body>
""")

    # ── sections ──────────────────────────────────────────────────────────────
    for key in schema.get("section_order", []):
        sec = schema.get("sections", {}).get(key)
        if not sec:
            continue
        ct    = sec.get("content_type", "text")
        field = sec.get("field", key)
        label = sec.get("label") or key.replace("_", " ").title()
        label_bold = sec.get("label_style", {}).get("bold", True)

        if ct == "text":
            parts.append(f'<div class="name">{_jinja_var(field)}</div>\n')

        elif ct == "paragraph":
            parts.append(_jinja_tag(f"if {field}"))
            parts.append("\n")
            parts.append(_section_header_html(label, label_bold))
            parts.append(f'<p class="body-text">{_jinja_var(field)}</p>\n')
            parts.append(_jinja_tag(f"endif"))
            parts.append("\n")

        elif ct == "bullet_list":
            item_fmt = sec.get("item_format", "")
            import re
            if item_fmt and re.search(r"\{\w+\}", item_fmt):
                item_html = _item_fmt_to_jinja(item_fmt)
            else:
                item_html = _jinja_var("item")

            parts.append(_jinja_tag(f"if {field}"))
            parts.append("\n")
            parts.append(_section_header_html(label, label_bold))
            parts.append("<ul>\n")
            parts.append(_jinja_tag(f"for item in {field}"))
            parts.append("\n")
            parts.append(f"  <li>{item_html}</li>\n")
            parts.append(_jinja_tag("endfor"))
            parts.append("\n</ul>\n")
            parts.append(_jinja_tag("endif"))
            parts.append("\n")

        elif ct == "experience_blocks":
            ef = sec.get("entry_format", {})
            parts.append(_jinja_tag(f"if {field}"))
            parts.append("\n")
            parts.append(_section_header_html(label, label_bold))
            parts.append(_jinja_tag(f"for exp in {field}"))
            parts.append("\n")

            # company line
            company_fmt = ef.get("company_line", "{company}, {city}, {state}")
            company_html = _item_fmt_to_jinja(company_fmt).replace(
                "{" , "").replace("}", "")  # clean any leftover raw braces
            # rebuild properly
            company_html = re.sub(r"\{(\w+)\}", lambda m: _jinja_var("exp." + m.group(1)), company_fmt)
            parts.append(f'<p class="exp-company">{company_html}</p>\n')

            title_fmt = ef.get("title_line", "{job_title}")
            title_html = re.sub(r"\{(\w+)\}", lambda m: _jinja_var("exp." + m.group(1)), title_fmt)
            parts.append(f'<p class="exp-title">{title_html}</p>\n')

            date_fmt = ef.get("date_line", "{start_date} - {end_date}")
            date_html = re.sub(r"\{(\w+)\}", lambda m: _jinja_var("exp." + m.group(1)), date_fmt)
            parts.append(f'<p class="exp-dates">{date_html}</p>\n')

            parts.append(_jinja_tag("if exp.bullets"))
            parts.append("\n<ul>\n")
            parts.append(_jinja_tag("for b in exp.bullets"))
            parts.append(f"\n  <li>{_jinja_var('b')}</li>\n")
            parts.append(_jinja_tag("endfor"))
            parts.append("\n</ul>\n")
            parts.append(_jinja_tag("endif"))
            parts.append("\n")
            parts.append(_jinja_tag("endfor"))
            parts.append("\n")
            parts.append(_jinja_tag("endif"))
            parts.append("\n")

    parts.append("</body>\n</html>")
    return "".join(parts)


# ── Step 3: render HTML and convert to PDF ───────────────────────────────────

def render_to_html(html_template: str, candidate_json: dict) -> str:
    env = Environment()
    tpl = env.from_string(html_template)
    return tpl.render(**candidate_json)


def html_to_pdf(html_str: str) -> bytes:
    from weasyprint import HTML
    return HTML(string=html_str).write_pdf()


def _apply_fmt(fmt: str, item: dict) -> str:
    """Fill {placeholders} from item dict, then clean up separators left by empty values."""
    import re
    result = fmt
    for ph in re.findall(r"\{(\w+)\}", fmt):
        result = result.replace("{" + ph + "}", str(item.get(ph, "") or ""))
    # Collapse runs of separators produced by empty fields
    result = re.sub(r",\s*,+", ",", result)         # ", ," → ","
    result = re.sub(r",\s*-\s*(\d)", r"- \1", result)  # ", - 2019" → "- 2019"
    result = re.sub(r",\s*$", "", result)            # trailing ","
    result = re.sub(r"-\s*$", "", result)            # trailing "-"
    result = re.sub(r"\s{2,}", " ", result)          # multiple spaces
    return result.strip()


def json_to_docx(candidate_json: dict, field_schema: dict) -> bytes:
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    ALIGN = {
        "center":  WD_ALIGN_PARAGRAPH.CENTER,
        "left":    WD_ALIGN_PARAGRAPH.LEFT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
        "right":   WD_ALIGN_PARAGRAPH.RIGHT,
    }

    doc = Document()
    font_name = field_schema.get("font_family", "Times New Roman")

    # Clear default margins (1 inch all sides)
    from docx.shared import Inches
    section = doc.sections[0]
    section.top_margin    = Inches(1)
    section.bottom_margin = Inches(1)
    section.left_margin   = Inches(1)
    section.right_margin  = Inches(1)

    # Reset Normal style to plain
    normal = doc.styles["Normal"]
    normal.font.name = font_name
    normal.font.size = Pt(12)
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after  = Pt(0)

    # Calibri standard body size is 11pt; headers match
    body_size = Pt(11) if font_name.lower() == "calibri" else Pt(12)

    def _para(style="Normal", space_before=0, space_after=0):
        p = doc.add_paragraph(style=style)
        p.paragraph_format.space_before = Pt(space_before)
        p.paragraph_format.space_after  = Pt(space_after)
        p.paragraph_format.line_spacing = Pt(14)
        return p

    def _run(para, text, bold=False):
        r = para.add_run(text)
        r.bold = bold
        r.font.name = font_name
        r.font.size = body_size
        return r

    def _bullet_para(text, align=WD_ALIGN_PARAGRAPH.LEFT, space_before=0):
        """Manual bullet: avoids List Bullet style's fixed numbering XML indent."""
        from docx.shared import Inches
        p = doc.add_paragraph(style="Normal")
        p.paragraph_format.left_indent        = Inches(0.25)
        p.paragraph_format.first_line_indent  = Inches(-0.13)
        p.paragraph_format.space_before       = Pt(space_before)
        p.paragraph_format.space_after        = Pt(0)
        p.paragraph_format.line_spacing       = Pt(14)
        p.alignment = align
        _run(p, "•  " + text)   # • + two spaces then text
        return p

    import re
    first_section = True

    for key in field_schema.get("section_order", []):
        sec = field_schema.get("sections", {}).get(key)
        if not sec:
            continue

        ct         = sec.get("content_type", "text")
        field      = sec.get("field", key)
        label      = sec.get("label")
        label_bold = sec.get("label_style", {}).get("bold", True)
        val        = candidate_json.get(field)

        if val is None or val == "" or val == []:
            continue

        # ── candidate name (header) ───────────────────────────────────────────
        if ct == "text":
            p = _para(space_before=0, space_after=8)
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _run(p, str(val), bold=True)
            first_section = False
            continue

        # ── section header ─────────────────────────────────────────────────────
        # 12pt before each header gives the blank-line look matching the template
        if label:
            hp = _para(space_before=0 if first_section else 12, space_after=2)
            hp.alignment = WD_ALIGN_PARAGRAPH.LEFT
            _run(hp, label, bold=label_bold)
            first_section = False

        # ── section body ──────────────────────────────────────────────────────
        if ct == "paragraph":
            p = _para(space_before=0, space_after=0)
            p.alignment = ALIGN.get(sec.get("alignment", "justify"), WD_ALIGN_PARAGRAPH.JUSTIFY)
            _run(p, str(val))

        elif ct == "bullet_list":
            item_fmt = sec.get("item_format", "")
            align    = ALIGN.get(sec.get("alignment", "left"), WD_ALIGN_PARAGRAPH.LEFT)
            for item in val:
                if item_fmt and re.search(r"\{\w+\}", item_fmt) and isinstance(item, dict):
                    text = _apply_fmt(item_fmt, item)
                else:
                    text = str(item)
                _bullet_para(text, align=align)

        elif ct == "experience_blocks":
            ef            = sec.get("entry_format", {})
            bullets_align = ALIGN.get(ef.get("bullets_alignment", "justify"), WD_ALIGN_PARAGRAPH.JUSTIFY)
            company_fmt   = ef.get("company_line", "{company}, {city}, {state}")
            title_fmt     = ef.get("title_line",   "{job_title}")
            date_fmt      = ef.get("date_line",    "{start_date} - {end_date}")
            company_bold  = ef.get("company_style", {}).get("bold", True)
            title_bold    = ef.get("title_style",   {}).get("bold", True)
            date_bold     = ef.get("date_style",    {}).get("bold", True)

            for i, exp in enumerate(val):
                # 10pt gap separates jobs; no gap before the first one
                cp = _para(space_before=10 if i > 0 else 0, space_after=0)
                cp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _run(cp, _apply_fmt(company_fmt, exp), bold=company_bold)

                tp = _para(space_before=0, space_after=0)
                tp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _run(tp, _apply_fmt(title_fmt, exp), bold=title_bold)

                dp = _para(space_before=0, space_after=2)
                dp.alignment = WD_ALIGN_PARAGRAPH.LEFT
                _run(dp, _apply_fmt(date_fmt, exp), bold=date_bold)

                for bullet in (exp.get("bullets") or []):
                    _bullet_para(str(bullet), align=bullets_align)

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_mapping_log(candidate_json: dict, field_schema: dict) -> dict:
    log = {}
    for section_key, sec in field_schema.get("sections", {}).items():
        field = sec.get("field")
        if field:
            log[field] = {"section": section_key, "found": field in candidate_json}
    return log


# ── DB operations ─────────────────────────────────────────────────────────────

def load_template(conn, template_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT field_schema
            FROM rpotential.client_resume_templates
            WHERE template_id = %s AND is_active
        """, (template_id,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"template_id {template_id} not found or inactive")
        return row[0]


def load_resume_json(conn, resume_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT structured_json FROM rpotential.candidate_resumes WHERE resume_id = %s
        """, (resume_id,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"resume_id {resume_id} not found")
        return row[0]


def store_resume(conn, candidate_id: int,
                 file_bytes: bytes, file_type: str, filename: str,
                 structured_json: dict) -> int:
    with conn.cursor() as cur:
        # unset previous latest
        cur.execute("""
            UPDATE rpotential.candidate_resumes
               SET is_latest = FALSE
             WHERE candidate_id = %s AND is_latest = TRUE
        """, (candidate_id,))
        cur.execute("""
            INSERT INTO rpotential.candidate_resumes
                (candidate_id, source_file, source_file_type, source_filename,
                 structured_json, is_latest)
            VALUES (%s, %s, %s, %s, %s, TRUE)
            RETURNING resume_id
        """, (candidate_id, file_bytes, file_type, filename,
              json.dumps(structured_json)))
        resume_id = cur.fetchone()[0]
        conn.commit()
        return resume_id


def store_output(conn, candidate_id: int, resume_id: int, template_id: int,
                 submission_id, output_bytes: bytes, output_filename: str,
                 mapping_log: dict) -> int:
    with conn.cursor() as cur:
        file_type = 'pdf' if output_filename.endswith('.pdf') else 'docx'
        cur.execute("""
            INSERT INTO rpotential.resume_outputs
                (candidate_id, resume_id, template_id, submission_id,
                 output_file, output_file_type, output_filename, field_mapping)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING output_id
        """, (candidate_id, resume_id, template_id, submission_id,
              output_bytes, file_type, output_filename, json.dumps(mapping_log)))
        output_id = cur.fetchone()[0]
        conn.commit()
        return output_id


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--candidate-id",   type=int, help="Required only when storing results in DB")
    ap.add_argument("--template-id",    type=int, required=True)
    ap.add_argument("--resume-file",    help="Path to candidate's raw resume (PDF/DOCX)")
    ap.add_argument("--json-file",      help="Path to pre-extracted candidate JSON file (skips Claude extraction)")
    ap.add_argument("--resume-id",      type=int, help="Use already-parsed resume from DB")
    ap.add_argument("--submission-id",  type=int, help="Link output to a client_submissions.link_id")
    ap.add_argument("--output",          default="output_resume.docx", help="Output file path")
    ap.add_argument("--dry-run",         action="store_true", help="Print mapped JSON, skip DB writes")
    ap.add_argument("--max-skills",      type=int, default=6, help="Max skill bullet lines after consolidation (default 6)")
    ap.add_argument("--no-consolidate",  action="store_true", help="Skip skill consolidation, use raw skills list")
    ap.add_argument("--api-key",         default=os.environ.get("ANTHROPIC_API_KEY", ""), help="Anthropic API key")
    ap.add_argument("--dsn",             default=os.environ.get("DATABASE_URL", ""))
    args = ap.parse_args()

    if not args.dsn:
        sys.exit("--dsn required (or set DATABASE_URL)")
    if not args.resume_file and not args.json_file and not args.resume_id:
        sys.exit("Provide --resume-file, --json-file, or --resume-id")
    if args.resume_id and not args.candidate_id:
        sys.exit("--candidate-id required when using --resume-id")

    conn = psycopg.connect(args.dsn)

    # ── load template ─────────────────────────────────────────────────────
    print(f"Loading template {args.template_id}...")
    field_schema = load_template(conn, args.template_id)

    # ── get candidate structured JSON ─────────────────────────────────────
    resume_id = args.resume_id
    candidate_json = None

    if args.json_file:
        path = Path(args.json_file)
        candidate_json = normalize_json(json.loads(path.read_text()))
        print(f"Loaded and normalized JSON from {path.name}")
        if not args.dry_run and args.candidate_id:
            resume_id = store_resume(conn, args.candidate_id,
                                     None, "txt", path.name, candidate_json)
            print(f"Stored as resume_id = {resume_id}")

    elif args.resume_file:
        path = Path(args.resume_file)
        file_type = path.suffix.lstrip(".").lower()
        file_bytes = path.read_bytes()
        print(f"Extracting text from {path.name}...")
        text = extract_text_from_file(file_bytes, file_type)
        print("Calling Claude to extract structured resume JSON...")
        candidate_json = extract_resume_json(text)
        if not args.dry_run:
            resume_id = store_resume(conn, args.candidate_id,
                                     file_bytes, file_type, path.name, candidate_json)
            print(f"Stored as resume_id = {resume_id}")
    else:
        print(f"Loading resume_id {resume_id} from DB...")
        candidate_json = load_resume_json(conn, resume_id)

    # ── consolidate skills ────────────────────────────────────────────────
    raw_skills = candidate_json.get("skills", [])
    if raw_skills and not args.no_consolidate:
        print(f"Consolidating {len(raw_skills)} skills → max {args.max_skills} lines...")
        candidate_json = {**candidate_json,
                          "skills": consolidate_skills(raw_skills, max_lines=args.max_skills,
                                                       api_key=args.api_key or None)}
        print(f"  → {len(candidate_json['skills'])} skill entries")

    if args.dry_run:
        print("\n── Candidate JSON (after skill consolidation) ──────────────")
        print(json.dumps(candidate_json, indent=2))
        return

    # ── build HTML template and render ────────────────────────────────────
    print("Building HTML template from schema...")
    html_template = build_html_template(field_schema)

    print("Rendering...")
    filled_html = render_to_html(html_template, candidate_json)

    output_path = Path(args.output)
    suffix = output_path.suffix.lower()
    if suffix == ".pdf":
        print("Converting to PDF...")
        output_bytes = html_to_pdf(filled_html)
    elif suffix == ".docx":
        print("Building DOCX...")
        output_bytes = json_to_docx(candidate_json, field_schema)
    else:
        # Fall back to HTML — openable in Word or browser
        output_path = output_path.with_suffix(".html")
        output_bytes = filled_html.encode("utf-8")

    output_path.write_bytes(output_bytes)
    print(f"Written to {output_path}")

    # ── store output in DB (only if candidate_id provided) ───────────────
    mapping_log = build_mapping_log(candidate_json, field_schema)
    if args.candidate_id and resume_id:
        output_id = store_output(conn, args.candidate_id, resume_id, args.template_id,
                                 args.submission_id, output_bytes,
                                 output_path.name, mapping_log)
        print(f"Stored as output_id = {output_id}")

    print(f"\nDone. → {output_path}")


if __name__ == "__main__":
    main()
