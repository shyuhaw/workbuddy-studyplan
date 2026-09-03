"""Generate a clean professional PDF resume from markdown source.
Usage: python resume_to_pdf.py [--output <path>] [--desktop]
"""
import argparse
import sys
from fpdf import FPDF


class ResumePDF(FPDF):
    def __init__(self, font_name="yh"):
        super().__init__()
        self.set_auto_page_break(auto=False)
        self.add_page()
        self.set_margins(18, 18, 18)
        self.font = font_name  # "yh" for YaHei, or None for Helvetica

    def h2(self, title):
        y = self.get_y()
        self.set_font(self.font, "B", 13)
        self.set_text_color(35, 70, 120)
        self.cell(0, 9, title)
        self.ln(1)
        self.set_draw_color(35, 70, 120)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def h3(self, title):
        self.set_font(self.font, "B", 11)
        self.set_text_color(30, 30, 30)
        self.cell(0, 8, title)
        self.ln(1)

    def bold_label(self, label, value):
        self.set_font(self.font, "B", 9.5)
        self.set_text_color(60, 60, 60)
        w = self.get_string_width(label + ": ")
        self.cell(w, 5.5, label + ": ", align="L")
        self.set_font(self.font, "", 9.5)
        self.set_text_color(30, 30, 30)
        self.cell(0, 5.5, value)
        self.ln()

    def bullet(self, text):
        self.set_font(self.font, "", 9.5)
        self.set_text_color(30, 30, 30)
        self.cell(4, 5.5, chr(8226))
        self.multi_cell(self.w - self.r_margin - self.l_margin - 4, 5.5, text)
        self.ln(1)

    def para(self, text):
        self.set_font(self.font, "", 9.5)
        self.set_text_color(30, 30, 30)
        self.multi_cell(0, 5.5, text)
        self.ln(1)

    def hr(self):
        self.set_draw_color(200, 200, 200)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(3)

    def table(self, rows, cw):
        for ri, row in enumerate(rows):
            is_header = ri == 0
            self.set_font(self.font, "B" if is_header else "", 9)
            fill = (225, 233, 245) if is_header else ((242, 246, 252) if ri % 2 == 0 else (255, 255, 255))
            self.set_fill_color(*fill)
            self.set_text_color(40, 40, 40)
            for i, cell in enumerate(row):
                border = 1
                align = "C" if is_header else "L"
                self.cell(cw[i], 6, cell, border=border, fill=True, align=align)
            self.ln()
        self.ln(2)

    def footer(self):
        self.set_y(-12)
        self.set_font(self.font, "I", 7.5)
        self.set_text_color(160, 160, 160)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def build_resume(pdf, W):
    # Header
    pdf.set_font(pdf.font, "B", 22)
    pdf.set_text_color(25, 45, 85)
    pdf.cell(0, 11, "麦  当")
    pdf.ln(3)
    pdf.set_font(pdf.font, "", 10.5)
    pdf.set_text_color(80, 80, 80)
    pdf.cell(0, 6, "AI 应用工程师 | 27岁 | 本科 | 浙江温州")
    pdf.ln(6)

    pdf.bold_label("地址", "浙江省温州市瓯海区西堡锦园")
    pdf.bold_label("电话", "13666082113")
    pdf.bold_label("邮箱", "1262574730@qq.com")
    pdf.bold_label("作品集", "https://f135488e38db4446a7262c2b1b72c310.app.workbuddy.link")
    pdf.bold_label("GitHub", "https://github.com/shyuhaw/workbuddy-studyplan")
    pdf.ln(3)
    pdf.hr()

    # 求职意向
    pdf.h2("求职意向")
    pdf.bold_label("目标岗位", "AI 应用工程师 / AI 解决方案工程师")
    pdf.bold_label("期望城市", "深圳 / 杭州 / 上海")
    pdf.bold_label("到岗时间", "随时")
    pdf.ln(2)

    # 核心优势
    pdf.h2("核心优势")
    for t in [
        "真实企业场景：主导公司飞书工程项目管理系统从 0 到 1 搭建，覆盖 23 个业务模块、777 个自定义字段、40+ 用户",
        "端到端项目交付：独立完成「需求分析 → 系统设计 → 开发落地 → 效果评测 → 部署上线」全链路",
        "成本意识强：所有 AI 调用均计算成本，Agent 单封处理 ¥0.0008，20 条评测总成本 ¥0.015",
        "工程诚实：主动报告泛化水平（83%而非100%）、BM25 语义召回天花板等短板",
    ]:
        pdf.bullet(t)
    pdf.ln(2)

    # 技术栈
    pdf.h2("技术栈")
    tech_rows = [
        ["类别", "技术"],
        ["编程语言", "Python（主力）、JavaScript、SQL"],
        ["后端框架", "Django、Django REST Framework"],
        ["数据库", "MySQL"],
        ["AI/ML", "PyTorch、BERT、scikit-learn"],
        ["前端", "Bootstrap、Tailwind CSS、HTML/CSS/JS"],
        ["桌面应用", "PyQt5 / PySide6"],
        ["AI 生态", "LLM 调用（DeepSeek/OpenAI 兼容）、RAG、Agent 编排、MCP 协议"],
        ["协作平台", "飞书项目、多维表格、飞书智能体、Webhook 集成"],
        ["工具链", "Playwright、Git、Meegle CLI"],
    ]
    pdf.table(tech_rows, [28, W - 28])

    # 项目经历
    pdf.h2("项目经历")

    # 项目一
    pdf.h3("项目一：跨境客户邮件智能处理 Agent  ｜ 独立开发者  ｜ 2026年8月")
    pdf.para("外贸业务员每天面对几十封英文邮件，人工需分类→提取→决策→录入 CRM，耗时约 100 秒/封。")
    pdf.bold_label("技术方案", "双层架构（规则层+LLM兜底）；分类层 100%/83.3%；提取层 95%；纯规则决策，毫秒级")
    pdf.ln(1)
    rows1 = [
        ["指标", "结果"],
        ["单封处理提速", "80.9×（人工 102.9s → Agent 1.27s）"],
        ["每日节省", "50.8 分钟（日均 30 封假设）"],
        ["每月释放工时", "18.6 小时"],
        ["每月 LLM 成本", "¥1.3"],
        ["独立样本泛化", "分类 83.3%（非调优后 100%）"],
    ]
    pdf.table(rows1, [35, W - 35])
    pdf.bullet("自写 Okapi BM25 检索（约 30 行、零依赖）")
    pdf.bullet("投诉件取「缺陷量 260 sqm」而非订单总量，价格取「索赔额 2,400」——字段语义随邮件类型变化")
    pdf.bullet("完整审计日志，每步操作可追溯")
    pdf.hr()

    # 项目二
    pdf.h3("项目二：飞书工程项目管理系统（从 0 到 1 搭建） ｜ 主导设计+配置落地  ｜ 2026年8月")
    pdf.para("公司 30 年工装企业，线下 Excel 管理项目，效率低、风险不可控。")
    pdf.bold_label("技术方案", "23 个业务模块、777 个自定义字段、20 个流程节点、12 个角色、5 条自动化流程")
    pdf.ln(1)
    rows2 = [
        ["指标", "数值", "来源"],
        ["使用人数", "40 人", "业务侧反馈"],
        ["自动化流程", "5 条", "空间设置→自动化"],
        ["已办工作项", "400 条", "Meegle CLI 实拉"],
        ["待办工作项", "620 条", "Meegle CLI 实拉"],
        ["高风险预警", "4 个", "经营舱 KPI 卡实时暴露"],
    ]
    pdf.table(rows2, [35, 30, W - 65])
    pdf.bullet("推动「客户→商机→施工项目」数据关联，建立营销/设计/预算/商务/采购/项目经理/财务协作关系")
    pdf.bullet("经营舱 KPI 卡实时暴露 4 个高风险项，风险由事后追溯变为事前可见")
    pdf.bullet("用 Meegle CLI 实拉全部配置数据，所有数字可复现")
    pdf.hr()

    # 项目三
    pdf.h3("项目三：WorkBuddy 自研 Skill（方法论固化） ｜ 独立开发者  ｜ 2026年8月")
    pdf.para("将两天踩过的坑和方法论封装成 4 个可复用 Skill：")
    rows3 = [
        ["Skill", "功能"],
        ["pdf-batch-extractor", "批量提取 PDF 文字并提炼要点，输出汇总笔记"],
        ["rag-eval-harness", "RAG 检索评测脚手架（recall@K / P@1 / NDCG）"],
        ["llm-biz-benchmark", "AI 业务效果量化方法学（透明假设模型 + 保守下界）"],
        ["static-demo-deploy", "静态 Demo 部署（预跑内嵌法）"],
    ]
    pdf.table(rows3, [35, W - 35])
    pdf.hr()

    # 教育背景
    pdf.h2("教育背景")
    pdf.bold_label("学校", "厦门理工学院  |  软件工程专业  |  本科  |  2022.09 - 2026.06（应届）")
    pdf.ln(2)

    # 当前工作
    pdf.h2("当前工作")
    pdf.bold_label("公司", "温州世纪明珠建设集团有限公司  |  飞书系统运维工程师  |  2026.07 - 至今")
    pdf.ln(1)
    for t in [
        "主导飞书工程项目管理系统从 0 到 1 搭建，覆盖营销/设计/预算/商务/采购/项目经理/财务全链路",
        "配置字段管理、页面布局、表格视图、流程管理、角色权限、自动化流程",
        "建立数据标准、必填规则、成果附件、流程验收要求",
        "推动客户→商机→施工项目的数据关联与业务衔接",
    ]:
        pdf.bullet(t)
    pdf.hr()

    # 个人作品
    pdf.h2("个人作品")
    pdf.bold_label("在线作品集", "https://f135488e38db4446a7262c2b1b72c310.app.workbuddy.link")
    pdf.bold_label("GitHub", "https://github.com/shyuhaw/workbuddy-studyplan")
    pdf.ln(2)

    # 自我评价
    pdf.h2("自我评价")
    for t in [
        "工程能力强：Python 主力语言，独立完成从数据处理到前端展示的全链路开发",
        "产品思维：不追求技术炫技，而是解决真实业务痛点，算得过账",
        "持续学习：2 周时间掌握 RAG / Agent / MCP 全套技能，完成 3 个落地项目",
        "沟通协作：作为公司唯一技术人员，独立对接 6 部门需求，推动系统落地",
    ]:
        pdf.bullet(t)
    pdf.ln(2)

    # Footer note
    pdf.set_font(pdf.font, "I", 8.5)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 5,
        "附：JD 匹配度\n"
        "① RAG 全链路落地经验 → 项目一（BM25→混合→精排三轮递进）\n"
        "② 业务理解+效果量化 → 邮件 80.9× 提速、飞书 40 人使用\n"
        "③ 工程交付能力 → 2 个完整项目，代码可复现、数据可审计\n"
        "④ 成本意识 → 单封处理 ¥0.0008、20 条评测 ¥0.015\n"
        "我不是培训班出来的，我是真正在业务里摸爬滚打过的。",
    )


def main():
    parser = argparse.ArgumentParser(description="Generate PDF resume from template")
    parser.add_argument("--output", "-o", help="Output PDF path")
    parser.add_argument("--desktop", action="store_true", help="Output to Desktop")
    args = parser.parse_args()

    # Try to add Chinese font
    import os
    font_candidates = [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simsun.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    font_name = None
    pdf = ResumePDF(font_name=None)  # temp to add fonts
    for path in font_candidates:
        if os.path.exists(path):
            try:
                pdf.add_font("zh", "", path)
                pdf.add_font("zh", "B", path)
                pdf.add_font("zh", "I", path)
                pdf.font = "zh"  # actually switch to it
                font_name = "zh"
                print(f"Using font: {path}")
                break
            except Exception as e:
                print(f"Font {path} failed: {e}", file=sys.stderr)
                continue
    W = pdf.w - pdf.l_margin - pdf.r_margin
    build_resume(pdf, W)

    if args.output:
        out_path = args.output
    elif args.desktop:
        out_path = r"C:\Users\Administrator\Desktop\简历-麦当.pdf"
    else:
        out_path = r"C:\Users\Administrator\WorkBuddy\workbuddy学习\简历-麦当.pdf"

    pdf.output(out_path)
    print(f"PDF saved to: {out_path}")


if __name__ == "__main__":
    main()
