"""PDF-документы: предложение, подтверждение посредничества, путеводитель.

ВАЖНО (юридически): документы агента-посредника НЕ заменяют юридически значимые документы оператора
(Reisebestätigung/Rechnung, Sicherungsschein, Formblatt). В PDF это явно сказано. См. docs/04-LEGAL-DE.md.
"""
import os
import re
import secrets
from pathlib import Path
from xml.sax.saxutils import escape

from flask import current_app
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..extensions import db
from ..models import Document
from ..utils import make_slug, utcnow

_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]
_BOLD_CANDIDATES = [p.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf") for p in _FONT_CANDIDATES]
_registered = False


def _fonts() -> tuple[str, str]:
    global _registered
    if _registered:
        return "TP", "TP-Bold"
    custom = current_app.config.get("PDF_FONT_PATH")
    regular = next((p for p in ([custom] if custom else []) + _FONT_CANDIDATES if p and os.path.exists(p)), None)
    if not regular:
        return "Helvetica", "Helvetica-Bold"  # без кириллицы — установите fonts-dejavu-core
    bold = regular.replace("DejaVuSans.ttf", "DejaVuSans-Bold.ttf")
    pdfmetrics.registerFont(TTFont("TP", regular))
    pdfmetrics.registerFont(TTFont("TP-Bold", bold if os.path.exists(bold) else regular))
    pdfmetrics.registerFontFamily("TP", normal="TP", bold="TP-Bold", italic="TP", boldItalic="TP-Bold")
    _registered = True
    return "TP", "TP-Bold"


L = {
    "de": dict(proposal="Reiseangebot", confirmation="Bestätigung der Vermittlung", guide="Reiseführer",
               for_customer="Für", date="Datum", valid="Angebot gültig bis", trip="Reise", destination="Reiseziel",
               period="Reisezeitraum", travellers="Reisende", adults="Erwachsene", children="Kinder",
               price="Preis", operator="Reiseveranstalter / Anbieter", includes="Enthaltene Leistungen",
               excludes="Nicht enthalten", ref="Referenz",
               role=("Wir vermitteln diese Reise als Reisebüro. Vertragspartner für die Reiseleistungen ist der oben "
                     "genannte Veranstalter/Anbieter. Dieses Dokument ist kein Reisevertrag."),
               price_note=("Preise sind Richtpreise vorbehaltlich Verfügbarkeit. Verbindlich sind erst die schriftliche "
                           "Reisebestätigung und die Rechnung des Veranstalters."),
               conf_note=("Die verbindliche Reisebestätigung, die Rechnung sowie – bei Pauschalreisen – der "
                          "Sicherungsschein werden vom Reiseveranstalter ausgestellt. Bitte prüfen Sie alle Angaben "
                          "und melden Sie Abweichungen umgehend."),
               guide_note=("Hinweis: Dieser Reiseführer ist eine allgemeine, KI-gestützte Information ohne Gewähr. "
                           "Aktuelle Einreise-, Visa- und Gesundheitsbestimmungen prüfen Sie bitte bei den offiziellen "
                           "Stellen (z. B. Auswärtiges Amt)."),
               status_paid="Zahlung eingegangen", page="Seite"),
    "en": dict(proposal="Travel proposal", confirmation="Confirmation of intermediary booking", guide="Travel guide",
               for_customer="For", date="Date", valid="Proposal valid until", trip="Trip", destination="Destination",
               period="Travel period", travellers="Travellers", adults="Adults", children="Children",
               price="Price", operator="Tour operator / provider", includes="Included", excludes="Not included",
               ref="Reference",
               role=("We arrange this trip as a travel agency. The contract for the travel services is concluded with "
                     "the operator/provider named above. This document is not a travel contract."),
               price_note=("Prices are indicative and subject to availability. Only the operator's written booking "
                           "confirmation and invoice are binding."),
               conf_note=("The binding booking confirmation, the invoice and – for package tours – the insolvency "
                          "protection certificate are issued by the tour operator. Please check all details and report "
                          "discrepancies immediately."),
               guide_note=("Note: this travel guide is general, AI-assisted information without guarantee. Please verify "
                           "current entry, visa and health requirements with official sources."),
               status_paid="Payment received", page="Page"),
    "ru": dict(proposal="Предложение по путешествию", confirmation="Подтверждение посреднической брони",
               guide="Путеводитель", for_customer="Для", date="Дата", valid="Предложение действительно до",
               trip="Поездка", destination="Направление", period="Период поездки", travellers="Путешественники",
               adults="Взрослые", children="Дети", price="Цена", operator="Туроператор / поставщик",
               includes="Включено", excludes="Не включено", ref="Номер",
               role=("Мы организуем эту поездку как турагентство. Договор на туристические услуги заключается с "
                     "указанным выше оператором/поставщиком. Этот документ не является договором о путешествии."),
               price_note=("Цены ориентировочные и зависят от наличия мест. Обязательны только письменное "
                           "подтверждение и счёт оператора."),
               conf_note=("Обязательное подтверждение бронирования, счёт и (для пакетных туров) Sicherungsschein "
                          "выдаёт туроператор. Проверьте все данные и сразу сообщите о расхождениях."),
               guide_note=("Примечание: путеводитель — общая информация, подготовленная с помощью ИИ, без гарантий. "
                           "Актуальные правила въезда, визы и здоровья проверяйте на официальных ресурсах."),
               status_paid="Оплата получена", page="Стр."),
    "uk": dict(proposal="Пропозиція подорожі", confirmation="Підтвердження посередницького бронювання",
               guide="Путівник", for_customer="Для", date="Дата", valid="Пропозиція дійсна до", trip="Подорож",
               destination="Напрямок", period="Період подорожі", travellers="Мандрівники", adults="Дорослі",
               children="Діти", price="Ціна", operator="Туроператор / постачальник", includes="Включено",
               excludes="Не включено", ref="Номер",
               role=("Ми організовуємо цю подорож як турагентство. Договір на туристичні послуги укладається з "
                     "зазначеним вище оператором/постачальником. Цей документ не є договором про подорож."),
               price_note=("Ціни орієнтовні та залежать від наявності місць. Обов'язкові лише письмове "
                           "підтвердження та рахунок оператора."),
               conf_note=("Обов'язкове підтвердження бронювання, рахунок і (для пакетних турів) Sicherungsschein "
                          "видає туроператор. Перевірте всі дані та одразу повідомте про розбіжності."),
               guide_note=("Примітка: путівник — загальна інформація, підготовлена за допомогою ШІ, без гарантій. "
                           "Актуальні правила в'їзду, візи та здоров'я перевіряйте на офіційних ресурсах."),
               status_paid="Оплату отримано", page="Стор."),
}


def _lbl(lang: str) -> dict:
    return L.get(lang) or L["de"]


def _styles(brand: str):
    reg, bold = _fonts()
    base = ParagraphStyle("base", fontName=reg, fontSize=10, leading=14, alignment=TA_LEFT)
    return {
        "base": base,
        "small": ParagraphStyle("small", parent=base, fontSize=8.5, leading=11, textColor=colors.HexColor("#555555")),
        "h1": ParagraphStyle("h1", parent=base, fontName=bold, fontSize=20, leading=24, textColor=colors.HexColor(brand), spaceAfter=6),
        "h2": ParagraphStyle("h2", parent=base, fontName=bold, fontSize=13, leading=17, spaceBefore=10, spaceAfter=4),
        "h3": ParagraphStyle("h3", parent=base, fontName=bold, fontSize=11, leading=15, spaceBefore=6, spaceAfter=2),
        "bold": ParagraphStyle("bold", parent=base, fontName=bold),
    }


def _p(text, style):
    return Paragraph(escape(str(text or "")).replace("\n", "<br/>"), style)


def _footer(agency, lang):
    reg, _ = _fonts()
    line1 = " · ".join(x for x in [agency.legal_name or agency.name, agency.street,
                                    f"{agency.zip_code or ''} {agency.city or ''}".strip()] if x)
    line2 = " · ".join(x for x in [agency.contact_email, agency.phone,
                                   f"USt-IdNr. {agency.vat_id}" if agency.vat_id else (agency.tax_number or "")] if x)

    def draw(canvas, doc):
        canvas.saveState()
        canvas.setFont(reg, 7.5)
        canvas.setFillColor(colors.HexColor("#666666"))
        canvas.drawString(20 * mm, 14 * mm, line1)
        canvas.drawString(20 * mm, 10 * mm, line2)
        canvas.drawRightString(190 * mm, 10 * mm, f"{_lbl(lang)['page']} {doc.page}")
        canvas.restoreState()
    return draw


def _out_path(agency) -> tuple[Path, str]:
    folder = Path(current_app.config["UPLOAD_DIR"]) / str(agency.id) / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(12) + ".pdf"
    return folder / name, name


def _build(agency, lang, flow_builder, title_key):
    path, name = _out_path(agency)
    st = _styles(agency.primary_color or "#0b6cff")
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=24 * mm, title=_lbl(lang)[title_key],
                            author=agency.legal_name or agency.name)
    flow = [_p(agency.name, st["h1"]), _p(_lbl(lang)[title_key], st["h2"]), Spacer(1, 4)]
    flow += flow_builder(st)
    doc.build(flow, onFirstPage=_footer(agency, lang), onLaterPages=_footer(agency, lang))
    return name


def _table(rows, st):
    data = [[_p(k, st["bold"]), _p(v, st["base"])] for k, v in rows if v not in (None, "")]
    t = Table(data, colWidths=[52 * mm, 118 * mm])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.HexColor("#dddddd")),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]))
    return t


def _booking_rows(booking, lead, lang):
    lb = _lbl(lang)
    period = " – ".join(d.strftime("%d.%m.%Y") for d in (booking.travel_start, booking.travel_end) if d)
    pax = f"{booking.pax_adults} {lb['adults']}" + (f", {booking.pax_children} {lb['children']}" if booking.pax_children else "")
    offer = booking.offer
    return [
        (lb["for_customer"], (lead.name if lead else "") or ""),
        (lb["trip"], booking.title), (lb["destination"], booking.destination),
        (lb["period"], period), (lb["travellers"], pax),
        (lb["operator"], booking.operator_name),
        (lb["includes"], offer.includes if offer else None), (lb["excludes"], offer.excludes if offer else None),
        (lb["price"], f"{booking.amount:,.2f} {booking.currency}".replace(",", " ")),
        (lb["valid"], offer.valid_until.strftime("%d.%m.%Y") if offer and offer.valid_until else None),
        (lb["ref"], f"#{booking.id}"),
    ]


def build_proposal(agency, booking, lead, lang):
    lb = _lbl(lang)

    def flow(st):
        return [_table(_booking_rows(booking, lead, lang), st), Spacer(1, 10),
                _p(lb["price_note"], st["small"]), Spacer(1, 4), _p(lb["role"], st["small"])]
    return _build(agency, lang, flow, "proposal")


def build_confirmation(agency, booking, lead, lang):
    lb = _lbl(lang)

    def flow(st):
        rows = _booking_rows(booking, lead, lang)
        if booking.operator_booking_ref:
            rows.append(("Ref. Veranstalter", booking.operator_booking_ref))
        if booking.paid_at:
            rows.append(("Status", f"{lb['status_paid']} ({booking.paid_at.strftime('%d.%m.%Y')})"))
        return [_table(rows, st), Spacer(1, 10), _p(lb["conf_note"], st["small"]), Spacer(1, 4), _p(lb["role"], st["small"])]
    return _build(agency, lang, flow, "confirmation")


_INLINE = [(re.compile(r"\*\*(.+?)\*\*"), r"<b>\1</b>"), (re.compile(r"(?<!\*)\*(?!\s)(.+?)\*(?!\*)"), r"<i>\1</i>"),
           (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r"\1 (\2)")]


def _inline(text: str) -> str:
    out = escape(text)
    for rx, repl in _INLINE:
        out = rx.sub(repl, out)
    return out


def build_markdown_pdf(agency, markdown_text: str, lang: str, title_key: str = "guide"):
    lb = _lbl(lang)

    def flow(st):
        items = []
        for raw in markdown_text.splitlines():
            line = raw.rstrip()
            if not line.strip():
                items.append(Spacer(1, 3))
            elif line.startswith("### "):
                items.append(Paragraph(_inline(line[4:]), st["h3"]))
            elif line.startswith("## "):
                items.append(Paragraph(_inline(line[3:]), st["h2"]))
            elif line.startswith("# "):
                items.append(Paragraph(_inline(line[2:]), st["h2"]))
            elif re.match(r"^\s*[-*]\s+", line):
                items.append(Paragraph("• " + _inline(re.sub(r"^\s*[-*]\s+", "", line)), st["base"]))
            else:
                items.append(Paragraph(_inline(line), st["base"]))
        items += [Spacer(1, 10), _p(lb["guide_note"], st["small"])]
        return items
    return _build(agency, lang, flow, title_key)


def save_document(agency, booking, kind: str, lang: str, filename: str) -> Document:
    base = make_slug(f"{kind}-{booking.id}", 40)
    doc = Document(agency_id=agency.id, booking_id=booking.id, kind=kind, language=lang, filename=filename,
                   download_name=f"{base}-{utcnow().strftime('%Y%m%d')}.pdf")
    db.session.add(doc)
    db.session.commit()
    return doc
