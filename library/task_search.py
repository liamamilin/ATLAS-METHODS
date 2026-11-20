"""Local catalogue search over the tasks already present in SkillRecords."""
import json
import re

from search import search_text
from taxonomy import term_pattern


TASK_SCHEMA = """
CREATE TABLE catalog_tasks (
    id INTEGER PRIMARY KEY, skill TEXT NOT NULL REFERENCES skills(id), position INTEGER NOT NULL,
    goal TEXT NOT NULL, applicability TEXT NOT NULL, outputs TEXT NOT NULL, tags TEXT NOT NULL
);
CREATE VIRTUAL TABLE task_terms USING fts5(goal, outputs, applicability, title, tags, content='');
"""

# Aliases supply common task phrasing and keep filter values stable.
ACTION_TAGS = {
    'transition': ('视图过渡', '视图过渡|页面过渡|过渡动画|视图转场|view transition|view transitions|shared element transition'),
    'slowquery': ('排查慢查询', '慢查询|数据库变慢|查询变慢|sql 性能|sql 优化|查询性能|sql 静态审计|sql 脚本运行静态审计|sql 反模式|slow query|slow queries|query optimization|query performance|sql performance|sql anti patterns'),
    'minutes': ('整理会议纪要', '会议纪要|会议记录|meeting minutes|meeting notes|minutes of meeting'),
    'contract': ('审阅合同', '合同审阅|审阅合同|审核合同|合同审核|合同风险|contract review|review contracts|contract risk'),
    'ci': ('配置 CI', '自动跑测试|自动化测试流水线|配置 ci|持续集成|ci pipeline|ci workflow|continuous integration|run tests on push'),
    'debug': ('排查故障', '故障排查|排查故障|调试|根因分析|debugging|troubleshooting|root cause analysis'),
    'test': ('编写测试', '编写测试|测试用例|单元测试|集成测试|端到端测试|unit test|unit tests|integration tests|end to end test|test cases'),
    'review': ('审查代码', '审查代码|代码审查|代码评审|code review|review code|pull request review'),
    'deploy': ('部署应用', '部署应用|部署服务|应用部署|上线应用|deploy an application|deploy application|application deployment|deploy a service'),
    'automate': ('自动化工作流', '工作流自动化|自动化工作流|自动化流程|workflow automation|automate a workflow|automated workflow'),
    'clean': ('清洗数据', '数据清洗|清洗数据|处理缺失值|data cleaning|clean data|data cleansing'),
    'analyze': ('分析数据', '数据分析|分析数据|探索性分析|data analysis|analyze data|exploratory analysis'),
    'write': ('撰写文档', '撰写文档|编写文档|写文档|文档编写|documentation writing|write documentation|draft a document'),
    'slides': ('制作演示文稿', '制作幻灯片|生成幻灯片|演示文稿|create slides|generate slides|presentation deck'),
    'image': ('生成图像', '生成图像|图像生成|生成图片|image generation|generate images|generate an image'),
    'design': ('设计界面', '设计界面|界面设计|交互设计|ui design|interface design|design an interface'),
    'plan': ('规划项目', '项目规划|规划项目|项目计划|项目排期|project planning|project plan'),
    'research': ('检索资料', '资料检索|检索资料|证据检索|文献检索|research sources|literature search|find evidence'),
}
TOOL_TAGS = {
    'react': ('React', 'react'),
    'nextjs': ('Next.js', 'next.js|nextjs|next js'),
    'postgresql': ('PostgreSQL', 'postgresql|postgres'),
    'sqlite': ('SQLite', 'sqlite'),
    'sql': ('SQL', 'sql'),
    'githubactions': ('GitHub Actions', 'github actions'),
    'python': ('Python', 'python'),
    'docker': ('Docker', 'docker'),
    'kubernetes': ('Kubernetes', 'kubernetes|k8s'),
    'playwright': ('Playwright', 'playwright'),
    'excel': ('Excel', 'excel|xlsx'),
    'gsheets': ('Google Sheets', 'google sheets|谷歌表格'),
    'notion': ('Notion', 'notion'),
    'slack': ('Slack', 'slack'),
    'figma': ('Figma', 'figma'),
}
# These aliases favor the requested work over tasks that only mention the same subject.
FOCUS_TERMS = {
    'transition': '加入视图过渡|实现视图过渡|引入视图过渡|add view transitions|implement view transitions',
    'slowquery': '排查慢查询|定位慢查询|慢查询诊断|sql 脚本运行静态审计|sql 反模式|diagnose slow queries|sql anti patterns|audit sql',
    'minutes': '生成会议纪要|起草会议纪要|撰写会议纪要|结构化会议纪要|整理会议纪要|draft meeting minutes|generate meeting minutes|write meeting minutes',
}
TAG_PATTERNS = {
    kind: {key: term_pattern(aliases) for key, (_, aliases) in tags.items()}
    for kind, tags in (('action', ACTION_TAGS), ('tool', TOOL_TAGS))
}
ANY_TAG_QUERY = 'tags:(' + ' OR '.join('tag' + key for key in (*ACTION_TAGS, *TOOL_TAGS)) + ')'
WORDS = re.compile(r'[\u3400-\u9fff]+|[a-z0-9]+')
STOP_WORDS = set('a an the and of to for by in at from with is be how do i can my need want help me it'.split())
FILLER = ('我想', '怎么', '一个', '一下', '可能', '需要', '导致', '请帮', '是否', '找出', '如何', '可以', '准备')


def matching_tags(text, kind):
    return [key for key, pattern in TAG_PATTERNS[kind].items() if pattern.search(text)]


def tokens(word):
    if word.isascii():
        return [word]
    return [word[i:i + 2] for i in range(len(word) - 1)] or [word]


def index_text(text):
    return ' '.join(term for word in WORDS.findall(search_text(text)) for term in tokens(word))


def add_tasks(con, skill, title, tasks):
    included = set()
    for position, task in enumerate(tasks, 1):
        goal = task['goal']
        if not goal:
            continue
        applicability = task.get('when') or ''
        outputs = task.get('outputs') or []
        outcomes = ' '.join(outputs)
        action = matching_tags(search_text(goal + ' ' + outcomes), 'action')
        tool = matching_tags(search_text(' '.join((title, goal, applicability, outcomes))), 'tool')
        tags = {'action': action, 'tool': tool}
        rowid = con.execute(
            'INSERT INTO catalog_tasks(skill, position, goal, applicability, outputs, tags) VALUES (?,?,?,?,?,?)',
            (skill, position, goal, applicability, json.dumps(outputs, ensure_ascii=False), json.dumps(tags)),
        ).lastrowid
        con.execute('INSERT INTO task_terms(rowid, goal, outputs, applicability, title, tags) VALUES (?,?,?,?,?,?)',
                    (rowid, index_text(goal), index_text(outcomes), index_text(applicability), index_text(title),
                     ' '.join('tag' + key for key in action + tool)))
        included.update(action)
        included.update(tool)
    return included


def tag_options(counts):
    return {kind: [{'id': key, 'label': label, 'count': counts[key]} for key, (label, _) in tags.items() if counts[key]]
            for kind, tags in (('action', ACTION_TAGS), ('tool', TOOL_TAGS))}


def available(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name = 'task_terms'").fetchone() is not None


def match_query(query, action='', tool=''):
    """Return candidate and preferred-task expressions for catalogue search."""
    filters = [f'tags:tag{key}' for key, tags in ((action, ACTION_TAGS), (tool, TOOL_TAGS)) if key in tags]
    actions = matching_tags(query, 'action')
    tools = matching_tags(query, 'tool')
    purpose = [f'{{goal outputs}}:"{index_text(term)}"'
               for key in dict.fromkeys([*actions, action])
               for term in FOCUS_TERMS.get(key, '').split('|') if term]
    focus = ' OR '.join(purpose) or None
    words = WORDS.findall(query)
    natural = len(words) >= 5 or sum(len(word) for word in words if not word.isascii()) >= 8
    if not actions and not natural:
        return ' AND '.join(filters), False, focus

    hints = []
    for word in words:
        if word.isascii():
            if len(word) > 1 and word not in STOP_WORDS:
                hints.append('"' + word + '"')
        elif len(word) >= 3:
            width = min(4, len(word))
            for start in range(len(word) - width + 1):
                part = word[start:start + width]
                if not any(filler in part for filler in FILLER):
                    hints.append('"' + ' '.join(tokens(part)) + '"')
    clues = ' OR '.join(dict.fromkeys(hints[:36]))
    if actions:
        required = ['(' + ' OR '.join('tags:tag' + key for key in actions) + ')']
        if tools:
            required.append('(' + ' OR '.join('tags:tag' + key for key in tools) + ')')
        concept = ' AND '.join(required)
        expr = f'({concept}) OR ({concept} AND ({clues}))' if clues else concept
    else:
        expr = clues
        if tools and expr:
            expr = '(' + ' OR '.join('tags:tag' + key for key in tools) + ') AND (' + expr + ')'
    if not expr:
        return ' AND '.join(filters), False, focus
    return ' AND '.join([*filters, '(' + expr + ')']), True, focus


TASK_HITS = """WITH focused AS MATERIALIZED (
    SELECT rowid FROM task_terms WHERE ? IS NOT NULL AND task_terms MATCH ?
), hits AS MATERIALIZED (
    SELECT t.id, t.skill, t.position, f.rowid IS NOT NULL AS preferred,
           bm25(task_terms, 5.0, 2.0, 0.75, 1.5, 1.0) AS score
    FROM task_terms JOIN catalog_tasks t ON t.id = task_terms.rowid
    LEFT JOIN focused f ON f.rowid = t.id
    WHERE task_terms MATCH ?
), best AS (
    SELECT *, row_number() OVER (PARTITION BY skill ORDER BY preferred DESC, score, position) AS choice FROM hits
)"""
