"""Build the approved customer-facing China2Go altitude preparation guide."""
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "output" / "pdf" / "china2go-altitude-guide.pdf"
FONT = Path("C:/Windows/Fonts/msjh.ttc")
FONT_BOLD = Path("C:/Windows/Fonts/msjhbd.ttc")


def register_fonts() -> None:
    pdfmetrics.registerFont(TTFont("JhengHei", str(FONT), subfontIndex=0))
    pdfmetrics.registerFont(TTFont("JhengHeiBold", str(FONT_BOLD), subfontIndex=0))


def build() -> None:
    register_fonts()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    navy = HexColor("#173F49")
    teal = HexColor("#28737A")
    orange = HexColor("#C85A27")
    ink = HexColor("#344E55")
    pale = HexColor("#EFF6F5")
    line = HexColor("#C8DAD8")

    doc = SimpleDocTemplate(
        str(OUTPUT), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=11 * mm, bottomMargin=10 * mm,
        title="西藏行前：高原與健康準備",
        author="China2Go 國旅環球",
    )
    styles = {
        "eyebrow": ParagraphStyle("eyebrow", fontName="JhengHei", fontSize=10, leading=14, textColor=HexColor("#BFD7D6")),
        "title": ParagraphStyle("title", fontName="JhengHei", fontSize=22, leading=28, textColor=HexColor("#FFFFFF")),
        "intro": ParagraphStyle("intro", fontName="JhengHei", fontSize=10.5, leading=16, textColor=ink),
        "heading": ParagraphStyle("heading", fontName="JhengHeiBold", fontSize=13, leading=17, textColor=navy, spaceAfter=2),
        "body": ParagraphStyle("body", fontName="JhengHei", fontSize=9.8, leading=15, textColor=ink),
        "small": ParagraphStyle("small", fontName="JhengHei", fontSize=8.5, leading=13, textColor=HexColor("#70868B")),
        "check": ParagraphStyle("check", fontName="JhengHei", fontSize=9.3, leading=14, textColor=ink),
        "badge": ParagraphStyle("badge", fontName="JhengHeiBold", fontSize=9.5, leading=14, textColor=orange, alignment=TA_CENTER),
    }

    hero = Table([[Paragraph("China2Go 國旅環球 / 行前資料", styles["eyebrow"]), ""],
                  [Paragraph("西藏行前：高原與健康準備", styles["title"]), ""]],
                 colWidths=[155 * mm, 0], rowHeights=[10 * mm, 22 * mm])
    hero.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), navy),
        ("SPAN", (0, 0), (1, 0)), ("SPAN", (0, 1), (1, 1)),
        ("LEFTPADDING", (0, 0), (-1, -1), 8 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))

    intro = Table([[Paragraph(
        "高原旅行準備得越具體，旅途中越從容。這份資料整理了兩條桃花行程的海拔差異、供氧安排、用藥準備，以及途中不舒服時的處理方式。",
        styles["intro"])]], colWidths=[159 * mm])
    intro.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), pale),
        ("BOX", (0, 0), (-1, -1), 0.6, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 7 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5 * mm),
    ]))

    def section(title: str, body: str):
        return KeepTogether([Paragraph(title, styles["heading"]), Paragraph(body, styles["body"]), Spacer(1, 2.5 * mm)])

    route_table = Table([
        [Paragraph("桃花9日", styles["badge"]), Paragraph("林芝進藏，不安排珠峰大本營；行程中的住宿及車輛有供氧安排。", styles["body"])],
        [Paragraph("桃花+珠峰11日", styles["badge"]), Paragraph("林芝進藏，後段前往珠峰；珠峰段入住絨布旅館，客房有供氧及獨立衛浴。", styles["body"])],
    ], colWidths=[37 * mm, 122 * mm])
    route_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, line),
        ("BACKGROUND", (0, 0), (0, -1), HexColor("#FFF5EF")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2 * mm),
    ]))

    checks = [
        "□ 將您選擇的行程、既有疾病與平時用藥整理好。",
        "□ 有心血管、呼吸系統、慢性病、懷孕或近期身體不適，出發前先向醫師詢問。",
        "□ 若考慮紅景天、丹木斯或其他產品，先請醫師或藥師評估並依建議使用。",
        "□ 準備保暖衣物、常用藥品及個人醫療資料；抵達後放慢節奏、留意身體反應。",
    ]
    checklist = Table([[Paragraph(item, styles["check"])] for item in checks], colWidths=[159 * mm])
    checklist.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), HexColor("#F8FAFA")),
        ("LINEBELOW", (0, 0), (-1, -2), 0.35, line),
        ("LEFTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5 * mm),
    ]))

    story = [
        hero, Spacer(1, 4 * mm), intro, Spacer(1, 4 * mm),
        Paragraph("先看您走哪一條行程", styles["heading"]), route_table, Spacer(1, 3 * mm),
        section("出發前，把個人狀況說清楚", "每個人對海拔的反應不同。若您有既有疾病、正在用藥，或近期身體狀況有變化，帶著行程內容向醫師說明，能得到較貼合您自己的準備建議。"),
        section("供氧安排怎麼用", "住宿及車輛的供氧設備是行程中的支援配置。需要使用時請直接告訴導遊，由導遊協助操作；若身體持續不舒服，會優先安排正規醫療協助。"),
        section("用藥準備", "紅景天、丹木斯或其他產品是否適合、怎麼使用，請依醫師或藥師對您個人狀況的建議準備，不照搬其他旅客的用法與劑量。"),
        section("旅途中不舒服怎麼辦", "請第一時間告訴導遊。導遊接受高原旅遊急救訓練，隨車備有血氧儀、氧氣瓶與急救包；需要就醫時，團隊會協助聯絡正規醫院或診所並安排交通。"),
        Paragraph("出發前快速檢查", styles["heading"]), checklist, Spacer(1, 4 * mm),
        Table([[Paragraph("China2Go 國旅環球 · 西藏行前服務資料 · 2026-09", styles["small"]),
                Paragraph("1 / 1", styles["small"])]], colWidths=[145 * mm, 14 * mm], style=TableStyle([
                    ("LINEABOVE", (0, 0), (-1, 0), 0.6, teal),
                    ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ])),
    ]
    doc.build(story)


if __name__ == "__main__":
    build()
