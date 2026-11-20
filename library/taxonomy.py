"""Provisional browse labels derived from the recorded category study."""
import re
import unicodedata


def fold(text):
    return unicodedata.normalize('NFKC', text).casefold()


# Labels originate in the 240-source review in docs/content-distribution-20261118.md.
CATEGORIES = (
    ('code', '软件开发与架构', '工程', 'javascript|typescript|python|rust|swift|swiftui|kotlin|react|flutter|vue|angular|framework|sdk|backend|frontend|refactor|software architecture|api design|rest api|microservice|codegen|design patterns|postgresql|mongodb|sqlite|database|graphql|sql|web development|git workflow|concurrency|编程|前端|后端|代码生成|重构|架构|数据库|并发|组件|接口设计|版本控制'),
    ('ai', 'AI、智能体与模型工程', '工程', 'agent|agentic|harness|prompt|llm|inference|fine tuning|fine-tuning|model training|automl|deep learning|machine learning|embeddings|rag|context engineering|agent memory|transformer|openai|claude code|model serving|checkpoint|token budget|mcp tools|智能体|代理框架|提示词|模型训练|大语言模型|微调|向量检索|推理部署|上下文|模型评估'),
    ('operations', '云、部署与运行维护', '工程', 'kubernetes|k8s|docker|terraform|cloud|deploy|deployment|devops|aws|azure|gcp|sre|ci/cd|github actions|observability|infrastructure|monitoring|otel|opentelemetry|kustomize|linux admin|systemd|运维|部署|容器|基础设施|可观测|监控|日志采集|故障恢复'),
    ('testing', '测试、调试与工程评审', '工程', 'unit test|integration test|e2e|playwright|debug|debugging|debugger|code review|pr review|regression test|test coverage|profiling|qa|tdd|benchmark testing|测试|调试|排错|代码审查|回归|性能分析|性能调优'),
    ('security', '网络安全与取证', '工程', 'cybersecurity|security audit|forensic|forensics|threat hunting|incident response|vulnerability|pentest|soc|hardening|malware|encryption|oauth|oidc|identity access|endpoint security|网络安全|取证|漏洞|安全审计|安全加固|加密|凭据管理|威胁|权限控制'),
    ('data', '数据获取、分析与统计', '工程', 'data analysis|analytics|data engineering|web scraping|scraper|etl|statistical|statistics|dataset|data pipeline|data quality|data visualization|pandas|warehouse|exploratory analysis|数据分析|数据采集|数据可视化|统计分析|数据质量|数据工程|数据清洗|爬取|数据仓库'),
    ('integration', '工具集成与自动化', '工程', ''),
    ('legal', '法律实务与合规', '专业', 'legal|compliance|litigation|lawyer|law|contract|antitrust|court|regulation|privacy law|recht|rechts|anwalt|bgb|zpo|vertrag|gerichts|rechtsstreit|法律|合同|诉讼|法规|合规|法务|隐私政策|商标|仲裁'),
    ('finance', '财税、会计与投资', '专业', 'accounting|accountant|tax|bookkeeping|investment|valuation|finance|financial|payroll|steuer|bilanz|finanz|buchhaltung|vat|xero|stocks|financial reporting|财税|会计|税务|投资|财务|工资单|对账|发票|银行|增值税'),
    ('research', '科研、教育与科学工作流', '专业', 'research methodology|scientific|academic|literature review|peer review|bioinformatics|genomics|experiment|course|curriculum|quiz|teaching|education|learning outcome|科学|科研|学术|教学|教育|实验|教案|课程|生物信息|文献综述'),
    ('engineering', '建筑、航天等专业工程', '专业', 'aerospace|astronautics|construction|civil engineering|structural engineering|robotics|manufacturing|mechanical engineering|建筑施工|航天|土木|机器人|制造'),
    ('business', '产品、营销与组织经营', '创作与办公', 'product management|marketing|sales|seo|growth marketing|brand|campaign|crm|gtm|competitor|customer acquisition|recruitment|roadmap|business plan|store optimization|health diagnostic|customer support|用户增长|产品管理|营销|销售|品牌|竞品|招聘|雇主|运营|商业计划|商机|客服|需求分析'),
    ('design', '视觉设计与音视频制作', '创作与办公', 'graphic design|visual design|design system|figma|ui design|ux design|image generation|video editing|music|audio|animation|illustration|typography|animation|color palette|accessibility design|视觉设计|音视频|视频|剪辑|音频|设计系统|字体|动画|配色|插画'),
    ('office', '办公、文档与协作工具', '创作与办公', 'spreadsheet|excel|powerpoint|pptx|docx|pdf|google drive|google sheets|google workspace|slides|notion|slack|calendar|email|meeting|document management|电子表格|幻灯片|文档编辑|办公|协作|会议|日程|邮件'),
    ('writing', '通用写作与内容编辑', '创作与办公', 'writing|copywriting|proofreading|translation|article|storytelling|newsletter|editorial|grammar|rewrite|blog post|写作|润色|翻译|文案|故事|文章|校对|剧本'),
    ('unfiled', '待归类', '整理中', ''),
)
LABELS = {key: label for key, label, _, _ in CATEGORIES}
BY_LABEL = {label: key for key, label, _, _ in CATEGORIES}
BY_LABEL['入口信息不足，暂不归类'] = 'unfiled'

PATTERNS = {}
for key, _, _, vocabulary in CATEGORIES:
    if not vocabulary:
        continue
    terms = sorted(set(vocabulary.split('|')), key=lambda term: (-len(term), term))
    expressions = [
        (r'(?<![a-z0-9])' + re.escape(term) + r'(?![a-z0-9])') if term.isascii() else re.escape(term)
        for term in terms
    ]
    PATTERNS[key] = re.compile('|'.join(expressions))


def term_score(pattern, text):
    return sum(2 if ' ' in term else 1 for term in set(pattern.findall(text)))


# These two curated source collections had consistent categories in the recorded sample.
# Use their specialization only when the Skill's own intent cannot separate categories.
SPECIALIZED_SOURCES = {
    'openaccountants/openaccountants': 'finance',
    'mukul975/anthropic-cybersecurity-skills': 'security',
}

# Apply specific title signals only to records the main rules could not place.
# Using MCP as a tool does not make an otherwise classified Skill an integration.
TITLE_HINTS = {
    'integration': re.compile(r'(?:automation|integration) via rube mcp|(?:自动化|集成).*rube mcp|rube mcp 自动化'),
    'code': re.compile(r'\bgit (?:worktree|branch|commit|rebase|merge)\b|\bconventional branch\b|\bcommit message\b|\bgit 分支|\bgit\b.*版本控制'),
    'ai': re.compile(r'\bskill creator\b|\bskill authoring\b|创建(?:与更新)?(?: codex| claude)? skill|编写(?: codex| claude)? skill'),
    'business': re.compile(r'\bseo\b|\baso\b|\bpricing strateg\w*\b|产品定价|定价策略|冷触达|邮件营销'),
    'office': re.compile(r'\bgoogle slides\b|\bpptx\b|演示文稿生成|幻灯片生成'),
    'design': re.compile(r'\bdesign system\b|\bbrand tokens\b|\bfigma\b|\btypography\b|\bcolor palette\b|设计系统|排版规范|视觉规范'),
    'writing': re.compile(r'\bcopy edit(?:ing)?\b|\bproofreading\b|写作风格|文案改写|文字润色'),
    'data': re.compile(r'\bdata quality\b|\bdata cleaning\b|数据质量|数据清洗|统计检验'),
}

LEGAL_TOPICS = {
    'antitrust': '竞争法', 'contracts': '合同', 'corporate': '公司治理',
    'criminal': '刑事', 'data-protection': '数据保护', 'employment': '劳动与雇佣',
    'employee-benefits': '员工福利', 'litigation': '诉讼', 'tax': '税务',
    'insolvency': '破产与重整', 'intellectual-property': '知识产权',
    'real-estate': '不动产', 'family': '家庭', 'public-law': '公法',
}


def classify(source, content, reviewed_label=None):
    if reviewed_label is not None:
        return reviewed_label, 'sample'
    if source['repository'].casefold() == 'thomasmoreai/legal-skills-open':
        parts = source['skill_path'].split('/')
        if len(parts) >= 4 and parts[2] == 'skills':
            return ('finance' if parts[1] == 'tax' else 'legal'), 'source'
    tasks = content.get('tasks') or []
    title = fold(' '.join([content.get('title') or '', content.get('name') or '']).replace('-', ' ').replace('_', ' '))
    intent = fold(' '.join((task.get('goal') or '') for task in tasks[:2]))
    summary = fold((content.get('summary') or '')[:1800])
    scores = {}
    for category, pattern in PATTERNS.items():
        scores[category] = (min(term_score(pattern, title), 6) * 4
                            + min(term_score(pattern, intent), 5) * 2
                            + min(term_score(pattern, summary), 4))
    ranked = sorted(scores, key=scores.get, reverse=True)
    first, second = ranked[:2]
    if scores[first] < 5 or scores[first] - scores[second] < 2:
        fallback = SPECIALIZED_SOURCES.get(source['repository'].casefold())
        if fallback:
            return fallback, 'source'
        hints = [category for category, pattern in TITLE_HINTS.items()
                 if pattern.search(fold(content.get('title') or content.get('name') or ''))]
        if len(hints) == 1:
            return hints[0], 'title'
        return 'unfiled', 'unfiled'
    return first, 'rule'


def topic_for(source):
    if source['repository'].casefold() == 'thomasmoreai/legal-skills-open':
        parts = source['skill_path'].split('/')
        if len(parts) >= 4 and parts[2] == 'skills':
            return LEGAL_TOPICS.get(parts[1], parts[1].replace('-', ' '))
    return ''
