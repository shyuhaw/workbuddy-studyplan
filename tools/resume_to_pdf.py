"""Generate a clean professional PDF resume using reportlab."""
import argparse
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER


FONT_PATH = r"C:\Windows\Fonts\msyh.ttc"
pdfmetrics.registerFont(TTFont("YaHei", FONT_PATH))
pdfmetrics.registerFont(TTFont("YaHeiBold", FONT_PATH))

DARK_BLUE  = HexColor("#1A3A6E")
ACCENT     = HexColor("#3B82C4")
GREY_LABEL = HexColor("#555555")
GREY_TEXT  = HexColor("#333333")
GREY_LIGHT = HexColor("#F0F4F8")
GREY_LINE  = HexColor("#DDDDDD")
WHITE      = HexColor("#FFFFFF")
SUBTLE     = HexColor("#999999")


def styles():
    s = getSampleStyleSheet()
    for name in list(s.byName.keys()):
        s[name].fontName = "YaHei"
    def add(name, **kw):
        try:
            s.add(ParagraphStyle(name=name, **kw))
        except KeyError:
            st = s[name]
            for k, v in kw.items():
                setattr(st, k, v)
    add("Title",   parent=s["Heading1"], fontName="YaHeiBold", fontSize=26, leading=32,
        textColor=DARK_BLUE, spaceAfter=4, alignment=TA_CENTER)
    add("Sub",     parent=s["Heading2"], fontName="YaHei",     fontSize=10, leading=13,
        textColor=ACCENT,    spaceAfter=6, alignment=TA_CENTER)
    add("H2",      parent=s["Heading2"], fontName="YaHeiBold", fontSize=12, leading=16,
        textColor=DARK_BLUE, spaceBefore=8, spaceAfter=3)
    add("H3",      parent=s["Heading3"], fontName="YaHeiBold", fontSize=10, leading=14,
        textColor=GREY_TEXT, spaceBefore=5, spaceAfter=2)
    add("Body",    parent=s["Normal"],   fontName="YaHei",     fontSize=9,  leading=13,
        textColor=GREY_TEXT, spaceAfter=1)
    add("Bullet",  parent=s["Normal"],   fontName="YaHei",     fontSize=9,  leading=13,
        textColor=GREY_TEXT, leftIndent=12, spaceAfter=1)
    add("Info",    parent=s["Normal"],   fontName="YaHei",     fontSize=9,  leading=13,
        textColor=GREY_TEXT, spaceAfter=1)
    add("Small",   parent=s["Normal"],   fontName="YaHei",     fontSize=8,  leading=11,
        textColor=SUBTLE,   spaceAfter=1)
    return s


def tstyle():
    return TableStyle([
        ("FONTNAME",    (0,0), (-1,-1), "YaHei"),
        ("FONTSIZE",    (0,0), (-1,-1), 9),
        ("ALIGN",       (0,0), (-1,0),  "CENTER"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("BACKGROUND",  (0,0), (-1,0),  GREY_LIGHT),
        ("TEXTCOLOR",   (0,0), (-1,0),  DARK_BLUE),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [WHITE, HexColor("#FAFCFE")]),
        ("GRID",        (0,0), (-1,-1), 0.5, GREY_LINE),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0), (-1,-1), 3),
        ("LEFTPADDING", (0,0), (-1,-1), 4),
        ("RIGHTPADDING",(0,0), (-1,-1), 4),
    ])


def tbl(rows, cw):
    from reportlab.platypus import Table
    data = [[Paragraph(c, styles()["Normal"]) for c in row] for row in rows]
    t = Table(data, colWidths=cw, repeatRows=1)
    t.setStyle(tstyle())
    return t


def build(s, W):
    story = []
    gap = lambda h=4: story.append(Spacer(1, h * mm))

    # Header
    story.append(Spacer(1, 5))
    story.append(Paragraph("麦  当", s["Title"]))
    story.append(Paragraph("AI 应用工程师 | 27岁 | 本科 | 浙江温州", s["Sub"]))
    gap(2)

    # Contact
    info = [
        ("地址", "浙江省温州市瓯海区西堡锦园"),
        ("电话", "13666082113"),
        ("邮箱", "1262574730@qq.com"),
        ("作品集", "https://f135488e38db4446a7262c2b1b72c310.app.workbuddy.link"),
        ("GitHub", "https://github.com/shyuhaw/workbuddy-studyplan"),
    ]
    for label, val in info:
        story.append(Paragraph(f"<b>{label}：</b>{val}", s["Info"]))
    gap(3)
    story.append(Table([[""]], colWidths=[W])
               ._render().getRenderOps() if False else None)  # skip, use HR instead
    from reportlab.platypus import HRFlowable
    story.append(HRFlowable(width="100%", thickness=1.5, color=ACCENT, spaceAfter=4))
    gap(2)

    # 求职意向
    story.append(Paragraph("求职意向", s["H2"]))
    for k,v in [("目标岗位","AI 应用工程师 / AI 解决方案工程师"),
                 ("期望城市","深圳 / 杭州 / 上海"),
                 ("到岗时间","随时")]:
        story.append(Paragraph(f"<b>{k}：</b>{v}", s["Info"]))
    gap(2)

    # 核心优势
    story.append(Paragraph("核心优势", s["H2"]))
    for t in [
        "真实企业场景：主导公司飞书工程项目管理系统从 0 到 1 搭建，覆盖 23 个业务模块、777 个自定义字段、40+ 用户",
        "端到端项目交付：独立完成「需求分析 → 系统设计 → 开发落地 → 效果评测 → 部署上线」全链路",
        "成本意识强：所有 AI 调用均计算成本，Agent 单封处理 ¥0.0008，20 条评测总成本 ¥0.015",
        "工程诚实：主动报告泛化水平（83%而非100%）、BM25 语义召回天花板等短板",
    ]:
        story.append(Paragraph(f"• {t}", s["Bullet"]))
    gap(2)

    # 技术栈
    story.append(Paragraph("技术栈", s["H2"]))
    tech = [
        ["类别","技术"],
        ["编程语言","Python（主力）、JavaScript、SQL"],
        ["后端框架","Django、Django REST Framework"],
        ["数据库","MySQL"],
        ["AI/ML","PyTorch、BERT、scikit-learn"],
        ["前端","Bootstrap、Tailwind CSS、HTML/CSS/JS"],
        ["桌面应用","PyQt5 / PySide6"],
        ["AI 生态","LLM 调用（DeepSeek/OpenAI 兼容）、RAG、Agent 编排、MCP 协议"],
        ["协作平台","飞书项目、多维表格、飞书智能体、Webhook 集成"],
        ["工具链","Playwright、Git、Meegle CLI"],
    ]
    story.append(tbl(tech, [25*mm, W-25*mm]))
    gap(2)

    # 项目经历
    story.append(Paragraph("项目经历", s["H2"]))

    # 项目一
    story.append(Paragraph("项目一：跨境客户邮件智能处理 Agent　独立开发者　2026年8月", s["H3"]))
    story.append(Paragraph("外贸业务员每天面对几十封英文邮件，人工需分类→提取→决策→录入 CRM，耗时约 100 秒/封。", s["Body"]))
    story.append(Paragraph("<b>技术方案</b>：双层架构（规则层+LLM兜底）；分类层 100%/83.3%；提取层 95%；纯规则决策，毫秒级", s["Info"]))
    gap(1)
    r1 = [
        ["指标","结果"],
        ["单封处理提速","80.9×（人工 102.9s → Agent 1.27s）"],
        ["每日节省","50.8 分钟（日均 30 封假设）"],
        ["每月释放工时","18.6 小时"],
        ["每月 LLM 成本","¥1.3"],
        ["独立样本泛化","分类 83.3%（非调优后 100%）"],
    ]
    story.append(tbl(r1, [32*mm, W-32*mm]))
    gap(1)
    for t in [
        "自写 Okapi BM25 检索（约 30 行、零依赖）",
        "投诉件取「缺陷量 260 sqm」而非订单总量，价格取「索赔额 2,400」——字段语义随邮件类型变化",
        "完整审计日志，每步操作可追溯",
    ]:
        story.append(Paragraph(f"• {t}", s["Bullet"]))
    gap(3)
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE, spaceAfter=3))

    # 项目二
    story.append(Paragraph("项目二：飞书工程项目管理系统（从 0 到 1 搭建）　主导设计+配置落地　2026年8月", s["H3"]))
    story.append(Paragraph("公司 30 年工装企业，线下 Excel 管理项目，效率低、风险不可控。", s["Body"]))
    story.append(Paragraph("<b>技术方案</b>：23 个业务模块、777 个自定义字段、20 个流程节点、12 个角色、5 条自动化流程", s["Info"]))
    gap(1)
    r2 = [
        ["指标","数值","来源"],
        ["使用人数","40 人","业务侧反馈"],
        ["自动化流程","5 条","空间设置→自动化"],
        ["已办工作项","400 条","Meegle CLI 实拉"],
        ["待办工作项","620 条","Meegle CLI 实拉"],
        ["高风险预警","4 个","经营舱 KPI 卡实时暴露"],
    ]
    story.append(tbl(r2, [26*mm, 20*mm, W-46*mm]))
    gap(1)
    for t in [
        "推动「客户→商机→施工项目」数据关联，建立营销/设计/预算/商务/采购/项目经理/财务协作关系",
        "经营舱 KPI 卡实时暴露 4 个高风险项，风险由事后追溯变为事前可见",
        "用 Meegle CLI 实拉全部配置数据，所有数字可复现",
    ]:
        story.append(Paragraph(f"• {t}", s["Bullet"]))
    gap(3)
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE, spaceAfter=3))

    # 项目三
    story.append(Paragraph("项目三：WorkBuddy 自研 Skill（方法论固化）　独立开发者　2026年8月", s["H3"]))
    story.append(Paragraph("将两天踩过的坑和方法论封装成 4 个可复用 Skill：", s["Body"]))
    r3 = [
        ["Skill","功能"],
        ["pdf-batch-extractor","批量提取 PDF 文字并提炼要点，输出汇总笔记"],
        ["rag-eval-harness","RAG 检索评测脚手架（recall@K / P@1 / NDCG）"],
        ["llm-biz-benchmark","AI 业务效果量化方法学（透明假设模型 + 保守下界）"],
        ["static-demo-deploy","静态 Demo 部署（预跑内嵌法）"],
    ]
    story.append(tbl(r3, [35*mm, W-35*mm]))
    gap(3)
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE, spaceAfter=3))

    # 教育背景
    story.append(Paragraph("教育背景", s["H2"]))
    story.append(Paragraph("<b>学校</b>：厦门理工学院　软件工程专业　本科　2022.09 - 2026.06（应届）", s["Info"]))
    gap(3)

    # 当前工作
    story.append(Paragraph("当前工作", s["H2"]))
    story.append(Paragraph("<b>公司</b>：温州世纪明珠建设集团有限公司　飞书系统运维工程师　2026.07 - 至今", s["Info"]))
    gap(1)
    for t in [
        "主导飞书工程项目管理系统从 0 到 1 搭建，覆盖营销/设计/预算/商务/采购/项目经理/财务全链路",
        "配置字段管理、页面布局、表格视图、流程管理、角色权限、自动化流程",
        "建立数据标准、必填规则、成果附件、流程验收要求",
        "推动客户→商机→施工项目的数据关联与业务衔接",
    ]:
        story.append(Paragraph(f"• {t}", s["Bullet"]))
    gap(3)
    story.append(HRFlowable(width="100%", thickness=0.5, color=GREY_LINE, spaceAfter=3))

    # 个人作品
    story.append(Paragraph("个人作品", s["H2"]))
    story.append(Paragraph("<b>在线作品集</b>：https://f135488e38db4446a7262c2b1b72c310.app.workbuddy.link", s["Info"]))
    story.append(Paragraph("<b>GitHub</b>：https://github.com/shyuhaw/workbuddy-studyplan", s["Info"]))
    gap(3)

    # 自我评价
    story.append(Paragraph("自我评价", s["H2"]))
    for t in [
        "工程能力强：Python 主力语言，独立完成从数据处理到前端展示的全链路开发",
        "产品思维：不追求技术炫技，而是解决真实业务痛点，算得过账",
        "持续学习：2 周时间掌握 RAG / Agent / MCP 全套技能，完成 3 个落地项目",
        "沟通协作：作为公司唯一技术人员，独立对接 6 部门需求，推动系统落地",
    ]:
        story.append(Paragraph(f"• {t}", s["Bullet"]))
    gap(3)

    # Footer
    story.append(Paragraph(
        "<b>附：JD 匹配度</b><br/>"
        "① RAG 全链路落地经验 → 项目一（BM25→混合→精排三轮递进）<br/>"
        "② 业务理解+效果量化 → 邮件 80.9× 提速、飞书 40 人使用<br/>"
        "③ 工程交付能力 → 2 个完整项目，代码可复现、数据可审计<br/>"
        "④ 成本意识 → 单封处理 ¥0.0008、20 条评测 ¥0.015<br/><br/>"
        "<i>我不是培训班出来的，我是真正在业务里摸爬滚打过的。</i>",
        s["Body"]
    ))
    return story


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", "-o")
    parser.add_argument("--desktop", action="store_true")
    args = parser.parse_args()

    s = styles()
    W = A4[0] - 36*mm
    story = build(s, W)

    out = args.output or (
        r"C:\Users\Administrator\Desktop\简历-麦当.pdf" if args.desktop
        else r"C:\Users\Administrator\WorkBuddy\workbuddy学习\简历-麦当.pdf"
    )
    doc = SimpleDocTemplate(out, pagesize=A4,
        leftMargin=18*mm, rightMargin=18*mm,
        topMargin=18*mm, bottomMargin=18*mm)
    doc.build(story)
    print(f"OK: {out}")

if __name__ == "__main__":
    main()
