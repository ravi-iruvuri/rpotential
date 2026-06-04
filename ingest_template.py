"""
ingest_template.py — Parse a client resume template (PDF or DOCX), extract
its field schema via Claude, generate a docxtpl DOCX with placeholders, and
store everything in client_resume_templates.

Usage:
    # Ingest a new template
    ./myenv/bin/python ingest_template.py \
        --file "Google Data Center Resume Format.pdf" \
        --client "Google" \
        --name "Data Center Format v1" \
        --dsn 'postgresql://...'

    # Regenerate the docx_template from an existing field_schema (no Claude call)
    ./myenv/bin/python ingest_template.py \
        --regen --template-id 1 \
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
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ── Alignment helpers ────────────────────────────────────────────────────────

ALIGN_MAP = {
    "center":  WD_ALIGN_PARAGRAPH.CENTER,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    "left":    WD_ALIGN_PARAGRAPH.LEFT,
    "right":   WD_ALIGN_PARAGRAPH.RIGHT,
}


def _set_align(paragraph, alignment: str):
    paragraph.alignment = ALIGN_MAP.get(alignment, WD_ALIGN_PARAGRAPH.LEFT)


def _bold_run(paragraph, text: str):
    run = paragraph.add_run(text)
    run.bold = True
    return run


def _add_bullet(doc: Document, text: str, alignment: str = "left"):
    p = doc.add_paragraph(style="List Bullet")
    _set_align(p, alignment)
    p.add_run(text)
    return p


# ── Step 1: extract text from source file ───────────────────────────────────

def extract_text(file_bytes: bytes, file_type: str) -> str:
    if file_type == "pdf":
        import pdfplumber
        with pdfplumber.open(BytesIO(file_bytes)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    elif file_type == "docx":
        doc = Document(BytesIO(file_bytes))
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        return file_bytes.decode("utf-8", errors="replace")


# ── Step 2: call Claude to extract field schema ──────────────────────────────

SCHEMA_PROMPT = """You are analyzing a client resume template.
Extract the complete structure as JSON matching EXACTLY this schema:

{
  "font_family": "<font name>",
  "section_order": ["<section_key>", ...],
  "sections": {
    "<section_key>": {
      "label": "<header text or null if no header>",
      "label_style": {"bold": true/false, "alignment": "left|center|right"},
      "content_type": "text|paragraph|bullet_list|experience_blocks",
      "alignment": "left|center|right|justify",
      "field": "<json_field_name>",
      "item_format": "<format string with {placeholders} — for bullet_list only>",
      "entry_format": {
        "company_line": "<format string>",
        "company_style": {"bold": true/false},
        "title_line": "<format string>",
        "title_style": {"bold": true/false},
        "date_line": "<format string>",
        "date_style": {"bold": true/false},
        "bullets_alignment": "left|justify"
      }
    }
  }
}

Notes:
- section_key values: header, summary, education, skills, certifications, work_experience
- content_type "experience_blocks" is for work history with company/title/dates/bullets
- entry_format is only present for experience_blocks sections
- item_format uses {placeholders} matching the candidate JSON field names
- Return ONLY valid JSON, no markdown fences, no explanation.

Template text:
"""


def extract_schema_via_claude(text: str) -> dict:
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{"role": "user", "content": SCHEMA_PROMPT + text}],
    )
    raw = msg.content[0].text.strip()
    return json.loads(raw)


# ── Step 3: generate docxtpl DOCX from schema ────────────────────────────────

def build_docx_template(schema: dict) -> bytes:
    """
    Produce a docxtpl-compatible DOCX with Jinja2 placeholders.
    The rendered document will have identical structure/formatting to the
    source template; only the content changes per candidate.
    """
    doc = Document()

    # global font
    font_name = schema.get("font_family", "Times New Roman")
    style = doc.styles["Normal"]
    style.font.name = font_name
    style.font.size = Pt(12)

    for section_key in schema.get("section_order", []):
        sec = schema["sections"].get(section_key)
        if not sec:
            continue

        ct = sec.get("content_type", "text")
        label = sec.get("label")
        label_style = sec.get("label_style", {})
        alignment = sec.get("alignment", "left")

        # ── section header ────────────────────────────────────────────────
        if label:
            hp = doc.add_paragraph()
            _set_align(hp, label_style.get("alignment", "left"))
            run = hp.add_run(label)
            run.bold = label_style.get("bold", True)

        # ── section body ──────────────────────────────────────────────────
        if ct == "text":
            # e.g. candidate name at top
            p = doc.add_paragraph()
            _set_align(p, alignment)
            p.add_run("{{ " + sec["field"] + " }}")

        elif ct == "paragraph":
            p = doc.add_paragraph()
            _set_align(p, alignment)
            p.add_run("{{ " + sec["field"] + " }}")

        elif ct == "bullet_list":
            field = sec["field"]
            item_fmt = sec.get("item_format", "{item}")
            import re
            jinja_item = item_fmt
            for ph in re.findall(r"\{(\w+)\}", item_fmt):
                jinja_item = jinja_item.replace("{" + ph + "}", "{{ item." + ph + " }}")
            if jinja_item == item_fmt:
                jinja_item = "{{ item }}"

            # Each {%p %} tag must be on its own paragraph in docxtpl
            p_for = doc.add_paragraph(style="List Bullet")
            p_for.add_run("{%p for item in " + field + " %}")

            p_item = doc.add_paragraph(style="List Bullet")
            _set_align(p_item, alignment)
            p_item.add_run(jinja_item)

            p_end = doc.add_paragraph(style="List Bullet")
            p_end.add_run("{%p endfor %}")

        elif ct == "experience_blocks":
            ef = sec.get("entry_format", {})
            field = sec["field"]

            # Loop start (docxtpl paragraph-level tag)
            loop_p = doc.add_paragraph()
            loop_p.add_run("{%p for exp in " + field + " %}")

            # Company line
            cp = doc.add_paragraph()
            _set_align(cp, "left")
            cr = cp.add_run(_build_exp_line(ef.get("company_line", "{company}, {city}, {state}"), "exp"))
            cr.bold = ef.get("company_style", {}).get("bold", True)

            # Title line
            tp = doc.add_paragraph()
            _set_align(tp, "left")
            tr = tp.add_run(_build_exp_line(ef.get("title_line", "{job_title}"), "exp"))
            tr.bold = ef.get("title_style", {}).get("bold", True)

            # Date line
            dp = doc.add_paragraph()
            _set_align(dp, "left")
            dr = dp.add_run(_build_exp_line(ef.get("date_line", "{start_date} - {end_date}"), "exp"))
            dr.bold = ef.get("date_style", {}).get("bold", True)

            # Bullets — each {%p %} tag on its own paragraph
            bullets_align = ef.get("bullets_alignment", "left")
            bp_for = doc.add_paragraph(style="List Bullet")
            bp_for.add_run("{%p for b in exp.bullets %}")

            bp_item = doc.add_paragraph(style="List Bullet")
            _set_align(bp_item, bullets_align)
            bp_item.add_run("{{ b }}")

            bp_end = doc.add_paragraph(style="List Bullet")
            bp_end.add_run("{%p endfor %}")

            # Loop end
            end_p = doc.add_paragraph()
            end_p.add_run("{%p endfor %}")

        doc.add_paragraph()  # spacer between sections

    buf = BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _build_exp_line(fmt: str, var: str) -> str:
    """Convert '{company}, {city}, {state}' → '{{ exp.company }}, {{ exp.city }}, {{ exp.state }}'"""
    import re
    result = fmt
    for ph in re.findall(r"\{(\w+)\}", fmt):
        result = result.replace("{" + ph + "}", "{{ " + var + "." + ph + " }}")
    return result


# ── DB operations ─────────────────────────────────────────────────────────────

def upsert_template(conn, client_name: str, template_name: str,
                    source_bytes: bytes, source_type: str, source_filename: str,
                    schema: dict, docx_bytes: bytes):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO rpotential.client_resume_templates
                (client_name, template_name,
                 source_file, source_file_type, source_filename,
                 field_schema, docx_template)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (client_name, template_name)
            DO UPDATE SET
                source_file      = EXCLUDED.source_file,
                source_file_type = EXCLUDED.source_file_type,
                source_filename  = EXCLUDED.source_filename,
                field_schema     = EXCLUDED.field_schema,
                docx_template    = EXCLUDED.docx_template,
                updated_at       = now()
            RETURNING template_id
        """, (client_name, template_name,
              source_bytes, source_type, source_filename,
              json.dumps(schema), docx_bytes))
        row = cur.fetchone()
        conn.commit()
        return row[0]


def regen_docx(conn, template_id: int):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT field_schema FROM rpotential.client_resume_templates
            WHERE template_id = %s
        """, (template_id,))
        row = cur.fetchone()
        if not row:
            sys.exit(f"template_id {template_id} not found")
        schema = row[0]
        docx_bytes = build_docx_template(schema)
        cur.execute("""
            UPDATE rpotential.client_resume_templates
               SET docx_template = %s, updated_at = now()
             WHERE template_id = %s
        """, (docx_bytes, template_id))
        conn.commit()
        print(f"Regenerated docx_template for template_id={template_id}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file",        help="Path to PDF or DOCX template file")
    ap.add_argument("--client",      help="Client name (e.g. 'Google')")
    ap.add_argument("--name",        help="Template name (e.g. 'Data Center Format v1')")
    ap.add_argument("--regen",       action="store_true", help="Regen docx_template from stored schema")
    ap.add_argument("--template-id", type=int,            help="Used with --regen")
    ap.add_argument("--schema-only", action="store_true", help="Print extracted schema JSON and exit")
    ap.add_argument("--dsn",         default=os.environ.get("DATABASE_URL", ""),
                    help="Postgres DSN (or set DATABASE_URL env var)")
    args = ap.parse_args()

    if not args.dsn:
        sys.exit("--dsn required (or set DATABASE_URL)")

    conn = psycopg.connect(args.dsn)

    if args.regen:
        if not args.template_id:
            sys.exit("--template-id required with --regen")
        regen_docx(conn, args.template_id)
        return

    if not args.file:
        sys.exit("--file required")

    path = Path(args.file)
    file_type = path.suffix.lstrip(".").lower()
    if file_type not in ("pdf", "docx"):
        sys.exit("Only PDF and DOCX templates are supported")

    file_bytes = path.read_bytes()
    print(f"Extracting text from {path.name} ({file_type})...")
    text = extract_text(file_bytes, file_type)

    print("Calling Claude to extract field schema...")
    schema = extract_schema_via_claude(text)

    if args.schema_only:
        print(json.dumps(schema, indent=2))
        return

    print("Building docxtpl DOCX template from schema...")
    docx_bytes = build_docx_template(schema)

    client_name = args.client or "Unknown"
    template_name = args.name or path.stem

    print(f"Storing template [{client_name} / {template_name}] in DB...")
    tid = upsert_template(conn, client_name, template_name,
                          file_bytes, file_type, path.name,
                          schema, docx_bytes)
    print(f"Done. template_id = {tid}")
    print(f"Use with: transform_resume.py --candidate-id N --template-id {tid} --dsn ...")


if __name__ == "__main__":
    main()
