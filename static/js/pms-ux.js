/*!
 * pms-ux.js — PMS2 通用前端体验增强库（零依赖，原生 JS）
 * ---------------------------------------------------------------
 * 覆盖：容错模糊搜索 / 拼音匹配 / 命中高亮 / 搜索建议 / URL 状态同步 /
 *       滚动与已加载内容恢复 / 真链接增强 / 图片懒加载与占位 / 表单守卫
 *       （未保存提醒、草稿、实时校验、提交防重复、输入提示、键盘交互）
 *
 * 设计原则：
 *  1. 渐进增强：低风险能力（图片占位、Esc 关弹窗、提交防重复、自动聚焦）默认开启；
 *     需业务语义的能力用 data-ux-* 声明式 opt-in，绝不改变既有 DOM 行为。
 *  2. 单文件、UMD 导出：浏览器挂 window.PMSUX，Node 可 require（便于纯函数测试）。
 *  3. 所有文本写入 DOM 一律走 textContent 或 escapeHtml，杜绝 XSS。
 *
 * 引入方式：由 templates/_ux_assets.html 以 defer 方式统一注入。
 */
(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.PMSUX = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  /* ============================================================
   * 0. 通用工具
   * ============================================================ */

  function debounce(fn, wait) {
    var timer = null;
    function debounced() {
      var ctx = this;
      var args = arguments;
      if (timer) clearTimeout(timer);
      timer = setTimeout(function () {
        timer = null;
        fn.apply(ctx, args);
      }, wait);
    }
    debounced.cancel = function () {
      if (timer) clearTimeout(timer);
      timer = null;
    };
    return debounced;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  // 全角转半角 + 小写 + 去首尾空白；中文保持不变
  function normalize(text) {
    return String(text == null ? "" : text)
      .replace(/[\uFF01-\uFF5E]/g, function (ch) {
        return String.fromCharCode(ch.charCodeAt(0) - 0xfee0);
      })
      .replace(/\u3000/g, " ")
      .toLowerCase()
      .trim();
  }

  function clamp(n, lo, hi) {
    return Math.max(lo, Math.min(hi, n));
  }

  // 读取点号路径（如 "account.username" / "member.level_label"）。
  // PMS2 各管理中心的列定义大量使用嵌套路径，直接 item[path] 取不到值。
  function getPath(obj, path) {
    if (obj == null) return undefined;
    if (path == null || path === "") return undefined;
    var parts = String(path).split(".");
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur == null) return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }

  /* ============================================================
   * 1. 拼音映射（常用字；业务可用 registerPinyin 增补）
   * ------------------------------------------------------------
   * 采用「拼音 → 汉字串」紧凑编码，运行时构建 char → {full, initial} 反查表。
   * 覆盖高频姓名/酒店业务字；未收录字不参与拼音匹配，但不等长/精确匹配不受影响。
   * ============================================================ */

  var PINYIN_GROUPS = {
    a: "啊阿啊埃挨哎唉爱碍安岸按暗案昂奥澳",
    b: "八巴把爸吧白百摆败班般半办帮邦包保报宝抱杯北备背被本笨比笔必边变便表别宾冰并波播伯博不部步",
    c: "猜才材财采菜参餐藏操曹层差拆常场长厂畅超朝潮车陈晨成城程称承吃持尺赤充冲虫重抽仇出初除楚处川穿传船窗创春纯ci词此从村存错",
    d: "达答大代带待单但淡当刀导到道得德灯登等低底地弟店电订定东冬懂动都斗独度短段断队对多",
    e: "额饿恶恩而儿二",
    f: "发法番反饭方房防访放飞非肥费分风雨服付父负复傅",
    g: "改盖干感刚钢高告歌各给根更工公功共沟狗购够古谷股骨固故顾关观官管馆惯光广归贵国果过",
    h: "还海害含汉行好合何和河荷核黑很红后湖户花华划话欢环换黄回会婚混火或货",
    j: "机鸡计记家加价驾减检简建江将交角脚叫接街节结解今金紧进京经警净静九酒就居局举句具卷决绝军君",
    k: "卡开看康考靠科可克客空口扣库快宽况困",
    l: "拉来蓝老乐雷冷理李里力立利连联脸练凉两亮量林临灵领令流六楼路露旅绿乱论罗落",
    m: "妈麻马吗买麦满慢忙毛么没美门梦米密面民明名命摸木目母慕穆",
    n: "拿哪那乃奶南难内尼你年娘鸟您牛农暖",
    o: "哦欧偶",
    p: "排派盘旁跑朋碰批皮片品平评凭破普浦",
    q: "七齐其奇起企气汽恰 Qian千签前钱浅欠强桥巧亲侵青清情请秋求球区取全权泉",
    r: "然让热人认日荣如入瑞若",
    s: "三散色沙山衫商上少绍设社深申身神生胜师诗实识时使世市事试收手首受书舒熟数水说硕思斯四松送苏诉素算随孙",
    t: "他她它台太泰谈汤唐桃特提题体天田甜条铁厅听停挺通同童统桶涂土团推退托",
    w: "瓦外完万王网往忘危为维伟卫未位味温文闻问我握屋无五午舞务物",
    x: "西吸希息稀席习洗系细下夏先显险现线相香箱想向小晓效些歇协谢心新信星行形型醒姓幸修休秀需徐许续选雪寻询",
    y: "压呀烟严言颜眼演宴验阳扬羊杨洋样腰邀摇要钥也野业叶夜一衣医依仪宜移已以义益亿忆艺议易亦意因音银引印应英营影硬用优尤邮游友有又于余鱼雨语玉遇yuan源愿约月悦云运",
    z: "杂再在咱赞早造则怎增扎渣展占战张章长找赵照者这zhen真争整正政之知直执值职只纸志制治中终钟种重舟周州朱珠猪竹主住注专装庄状追准桌资子紫自字宗总纵走祖组醉最尊昨左作坐"
  };

  // 完整音节 → 常用字（覆盖高频姓名与酒店业务字），支持全拼输入
  var PINYIN_EXT = {
    // 常见音节全拼（只收新键，不覆盖 PINYIN_FULL 既有映射）
    feng: "冯峰锋逢封", cai: "蔡才材财采菜", hua: "华花化划画话",
    long: "龙隆聋拢", tao: "陶涛桃淘掏", gang: "刚钢岗纲", qiang: "强枪墙抢",
    xiu: "秀休修绣", ying: "英应影营迎赢映", jing: "静净京经惊井竞境",
    na: "娜那纳哪", juan: "娟卷捐绢", mei: "美梅每妹媒", lei: "雷蕾类累泪",
    xue: "雪学血削", ping: "平评瓶苹凭屏萍", hong: "红洪宏虹鸿",
    hui: "辉会回汇慧惠毁", ting: "婷停听厅庭挺", zi: "子字自紫资姿滋",
    ni: "尼泥你拟逆倪", yun: "云允运韵孕", nai: "乃奶耐奈",
    jiu: "酒就久九救究", can: "餐参残蚕惨", kan: "看刊砍坎", an: "安案按岸暗",
    quan: "全权劝券泉犬", xian: "现显先限县线险鲜闲",
    zhi: "制值职知直执纸志治只之支指", ji: "计记级即机基集积极技际",
    bao: "报保包宝抱薄", zong: "总综棕宗纵", fa: "法发罚阀乏",
    zhen: "真镇针阵珍震", liao: "料聊疗僚辽", dao: "道刀导到倒岛盗",
    chan: "产禅缠蝉", kuai: "快块筷", ban: "班般办板半伴版",
    she: "设社舍涉射蛇摄", qu: "去区取曲趋驱", shu: "书数属输树熟叔",
    shuo: "说硕朔", ce: "测册侧策厕", cha: "查茶察差插", gai: "改盖概该",
    chu: "出处初除础厨储楚", huan: "换环还缓幻唤患", shuang: "双霜爽",
    xia: "下夏吓侠虾", xi: "系西希息稀洗夕惜吸", bie: "别憋",
    duo: "多躲夺朵", zhong: "中重种终众忠钟", bo: "波博播伯泊薄",
    gan: "干感赶杆甘肝", zang: "脏藏葬", chou: "抽愁筹仇丑",
    kuang: "况矿框旷", sui: "虽岁随碎穗", bang: "帮邦绑磅棒",
    lang: "狼朗浪廊", dang: "当党挡荡", hen: "很恨狠痕", hai: "还海害嗨",
    bei: "被备北贝背倍", sai: "赛塞腮", xuan: "选宣悬旋",
    ti: "体提题替梯", heng: "横衡恒亨", zai: "在再载栽", ai: "爱矮埃挨",
    mian: "面眠棉免绵", lian: "连练联怜链帘", jue: "绝决觉掘爵",
    qie: "切且窃妾", shao: "少绍烧勺", chuan: "传穿船川串",
    chang: "长常厂场唱昌尝畅偿", qing: "清情请青轻晴氢倾",
    ju: "局举句具巨聚距剧居", huai: "坏淮槐", nuan: "暖", shui: "水谁睡税",
    bian: "边变便遍辨鞭", bin: "宾滨彬", bing: "病并冰兵", miao: "秒描庙妙苗",
    nie: "捏聂孽", pai: "排派牌", pang: "旁胖庞", pei: "陪配佩",
    qi: "其期奇齐起企气汽器", qiao: "桥巧敲侨", que: "却确缺雀",
    ran: "然燃染", rang: "让嚷", reng: "仍扔", rong: "容荣融绒", rou: "柔肉揉",
    sang: "桑丧嗓", sao: "扫骚", se: "色涩", sen: "森", sha: "沙杀纱",
    shai: "筛晒", shan: "山善闪扇删", shang: "上商伤赏尚", shei: "谁",
    sheng: "生声升省胜剩绳", shua: "刷耍", shuai: "帅摔甩", shuan: "拴栓",
    shun: "顺瞬", si: "四思司丝私死", sou: "搜", suan: "算酸",
    ta: "他她它塔踏", tai: "太台抬胎", tan: "谈探摊贪坛", teng: "疼腾",
    tiao: "条跳挑", tie: "铁贴", tong: "同通痛童桶铜统", tou: "头投偷",
    tu: "图土突途徒吐兔", tun: "屯吞", tuo: "脱托拖妥",
    wa: "瓦挖娃袜", wai: "外歪", wan: "完万晚弯玩碗", wen: "文闻温稳纹吻",
    weng: "翁", wo: "我卧握", xiang: "向相香想项象详", xin: "新心信欣辛薪芯",
    xun: "训询寻巡迅", ya: "呀压牙芽亚雅",
    yi: "一以已意易义医依仪益亿忆艺议亦异移翼",
    yong: "用永勇涌拥", you: "有又友游优忧幽", yue: "月约越悦阅",
    za: "杂砸", zan: "咱暂赞", zao: "早造遭糟枣", ze: "则责泽择",
    zei: "贼", zen: "怎", zha: "扎渣眨", zhai: "摘宅窄债",
    zhan: "站占展战粘", zhe: "这折者浙", zhua: "抓",
    zhuan: "专转赚传砖", zhuang: "装状庄壮撞", zhui: "追坠", zhun: "准",
    zhuo: "桌捉着卓", zu: "足族祖阻租", zuan: "钻", zui: "嘴最醉",
    zun: "尊遵", zuo: "作做坐左座昨"
  };

  var PINYIN_FULL = {
    zhang: "张章掌账障丈仗", jiang: "江将蒋奖讲", jia: "家加价驾假甲",
    jun: "军君均俊", li: "李理立力利丽历例礼", lin: "林临邻淋",
    ling: "林灵领令零龄", liu: "刘流留柳六", wang: "王网往忘望",
    zhao: "赵照招找兆", chen: "陈晨辰", yang: "杨羊阳洋扬样",
    huang: "黄皇煌", zhou: "周州舟", wu: "吴五午舞务物乌",
    xu: "徐许续需须", sun: "孙", ma: "马妈麻码", zhu: "朱珠猪竹主住注",
    hu: "胡湖户护虎", guo: "郭国果过锅", he: "何和河合贺", gao: "高告糕",
    luo: "罗洛落逻", zheng: "郑政正整争证", liang: "梁良两亮量辆",
    xie: "谢鞋协斜", song: "宋送松颂", tang: "唐汤糖躺",
    han: "韩汉含寒喊", cao: "曹操草槽", peng: "彭朋碰棚", zeng: "曾增",
    xiao: "肖萧笑小晓效", tian: "田天甜填", dong: "董东动冬懂",
    pan: "潘盘判", yu: "于余鱼雨语玉遇", du: "杜度读独督",
    ye: "叶夜业野", cheng: "程成城称承诚", su: "苏诉素速",
    wei: "魏韦伟卫为位未味维", lv: "吕旅绿律", ding: "丁定订顶",
    shen: "沈深身神审", ren: "任人认仁", yao: "姚要腰药摇",
    lu: "陆路露鲁卢", fan: "范饭反方房防访放", jin: "金今进近仅紧",
    qiu: "邱秋求球", hou: "侯后厚", zou: "邹走", xiong: "熊兄雄",
    meng: "孟梦", qin: "秦亲勤", bai: "白百摆败", cui: "崔",
    kang: "康", mao: "毛帽", qian: "钱前千签欠", yuan: "袁元源愿远园",
    deng: "邓等灯登", yan: "严颜眼演烟验证", shi: "史石时使事实市是式",
    gu: "顾古故谷股骨", fu: "傅富服付父负复福",
    // 酒店业务高频词
    ke: "客课刻克科", fang: "房方防访放芳", jian: "间建检简减健",
    dian: "店电点典", guan: "管关观官馆惯",
    ru: "如入", tui: "退推",
    dan: "单但淡蛋胆担",
    shou: "收手首受守", yin: "银音因引印", xing: "行形型醒姓",
    ming: "明名命鸣", chao: "超朝潮抄",
  };

  var charPinyin = Object.create(null);
  // 先以首字母表兜底（保证所有收录字至少可做首字母匹配）
  Object.keys(PINYIN_GROUPS).forEach(function (initial) {
    var chars = PINYIN_GROUPS[initial];
    for (var i = 0; i < chars.length; i++) {
      var ch = chars.charAt(i);
      if (!charPinyin[ch]) charPinyin[ch] = initial;
    }
  });
  // 再用完整音节覆盖（支持全拼）；EXT 只含新键，不会覆盖 PINYIN_FULL 既有映射
  [PINYIN_FULL, PINYIN_EXT].forEach(function (table) {
    Object.keys(table).forEach(function (syllable) {
      var chars = table[syllable];
      for (var j = 0; j < chars.length; j++) {
        if (/^[a-z]+$/.test(syllable)) charPinyin[chars.charAt(j)] = syllable;
      }
    });
  });

  // 业务增补拼音映射：registerPinyin({ '鑫': 'xin', ... })
  function registerPinyin(map) {
    Object.keys(map || {}).forEach(function (ch) {
      charPinyin[ch] = normalize(map[ch]);
    });
  }

  function pinyinOfChar(ch) {
    return charPinyin[ch] || "";
  }

  // 把字符串转拼音串，同时返回 char→[start,end] 的索引映射（用于高亮回映射）
  function pinyinize(text) {
    var full = "";
    var initials = "";
    var spans = []; // 每个字符对应 {full:[s,e], initial:[s,e]}
    for (var i = 0; i < text.length; i++) {
      var py = pinyinOfChar(text.charAt(i));
      if (py) {
        var fs = full.length;
        full += py;
        var is = initials.length;
        initials += py.charAt(0);
        spans.push({ full: [fs, full.length], initial: [is, initials.length] });
      } else {
        // 非中文（字母/数字）原样进入拼音串，便于中英混输
        var ch = text.charAt(i);
        var pos1 = full.length;
        full += ch.toLowerCase();
        var pos2 = initials.length;
        initials += ch.toLowerCase();
        spans.push({ full: [pos1, full.length], initial: [pos2, initials.length] });
      }
    }
    return { full: full, initials: initials, spans: spans };
  }

  /* ============================================================
   * 2. 相似度算法
   * ------------------------------------------------------------
   * 综合三种信号，返回 {score:0..1, ranges:[[start,end],...]}：
   *   - 精确子串（含大小写/全角归一）        高权重
   *   - 编辑距离（错字/漏字/多字）            中权重
   *   - 子序列（漏字、顺序一致）              低权重
   * 若 query 为纯字母，额外做拼音（全拼/首字母）匹配，命中映射回汉字区间。
   * ============================================================ */

  function levenshtein(a, b) {
    if (a === b) return 0;
    if (!a.length) return b.length;
    if (!b.length) return a.length;
    var prev = new Array(b.length + 1);
    var cur = new Array(b.length + 1);
    for (var j = 0; j <= b.length; j++) prev[j] = j;
    for (var i = 1; i <= a.length; i++) {
      cur[0] = i;
      for (var k = 1; k <= b.length; k++) {
        var cost = a.charAt(i - 1) === b.charAt(k - 1) ? 0 : 1;
        cur[k] = Math.min(cur[k - 1] + 1, prev[k] + 1, prev[k - 1] + cost);
      }
      var tmp = prev; prev = cur; cur = tmp;
    }
    return prev[b.length];
  }

  function mergeRanges(ranges) {
    if (!ranges.length) return [];
    var sorted = ranges.slice().sort(function (a, b) { return a[0] - b[0]; });
    var out = [sorted[0].slice()];
    for (var i = 1; i < sorted.length; i++) {
      var last = out[out.length - 1];
      var cur = sorted[i];
      if (cur[0] <= last[1]) {
        last[1] = Math.max(last[1], cur[1]);
      } else {
        out.push(cur.slice());
      }
    }
    return out;
  }

  // 子序列匹配：返回命中字符在原串中的索引区间
  function subsequenceRanges(query, text) {
    var qi = 0;
    var ranges = [];
    var first = -1;
    var last = -1;
    for (var i = 0; i < text.length && qi < query.length; i++) {
      if (text.charAt(i) === query.charAt(qi)) {
        if (first < 0) first = i;
        last = i;
        ranges.push([i, i + 1]);
        qi++;
      }
    }
    if (qi < query.length) return null;
    return { ranges: mergeRanges(ranges), first: first, last: last };
  }

  function fuzzyMatch(query, text) {
    var q = normalize(query);
    var t = normalize(text);
    var miss = { score: 0, ranges: [], via: "none" };
    if (!q) return miss;
    if (!t) return miss;

    // ① 精确子串
    var idx = t.indexOf(q);
    if (idx >= 0) {
      var posPenalty = Math.min(0.1, idx * 0.004);
      return { score: clamp(1 - posPenalty, 0.85, 1), ranges: [[idx, idx + q.length]], via: "exact" };
    }

    // ② 编辑距离：在等长滑动窗口上取最小距离（容许错字/漏字/多字）
    var best = null;
    var maxDist = Math.max(1, Math.floor(q.length / 3));
    for (var w = Math.max(1, q.length - 1); w <= Math.min(t.length, q.length + 1); w++) {
      for (var s = 0; s + w <= t.length; s++) {
        var window = t.substr(s, w);
        var dist = levenshtein(q, window);
        if (dist > maxDist) continue;
        var score = clamp(1 - dist / Math.max(q.length, 2) - s * 0.004, 0.35, 0.84);
        if (!best || score > best.score) best = { score: score, ranges: [[s, s + w]], via: "edit" };
      }
    }

    // ③ 子序列（漏字、顺序一致）
    var sub = subsequenceRanges(q, t);
    if (sub) {
      var span = sub.last - sub.first + 1;
      var density = q.length / Math.max(span, 1);
      var subScore = clamp(0.3 + density * 0.42 - sub.first * 0.002, 0.25, 0.72);
      if (!best || subScore > best.score) {
        best = { score: subScore, ranges: sub.ranges, via: "subseq" };
      }
    }

    // ④ 拼音匹配（query 为纯字母且与文本不完全相同时）
    if (/^[a-z0-9]+$/.test(q) && /[\u4e00-\u9fff]/.test(text)) {
      var py = pinyinize(text);
      var hit = matchInPinyin(q, py);
      if (hit && (!best || hit.score > best.score)) {
        return hit;
      }
    }

    return best || miss;
  }

  function matchInPinyin(query, py) {
    // 全拼子串
    var fullIdx = py.full.indexOf(query);
    if (fullIdx >= 0) {
      return {
        score: 0.82,
        ranges: spansToCharRanges(py.spans, fullIdx, fullIdx + query.length, "full"),
        via: "pinyin-full"
      };
    }
    // 首字母子串
    var iniIdx = py.initials.indexOf(query);
    if (iniIdx >= 0) {
      return {
        score: 0.74,
        ranges: spansToCharRanges(py.spans, iniIdx, iniIdx + query.length, "initial"),
        via: "pinyin-initial"
      };
    }
    // 首字母子序列（如 "zj" 匹配 "张军"）
    var sub = subsequenceRanges(query, py.initials);
    if (sub) {
      var ranges = [];
      sub.ranges.forEach(function (r) {
        ranges = ranges.concat(spansToCharRanges(py.spans, r[0], r[1], "initial"));
      });
      return { score: 0.6, ranges: mergeRanges(ranges), via: "pinyin-initial-subseq" };
    }
    return null;
  }

  // 拼音串区间 → 原文字符区间（用于高亮汉字，而非拼音字母）
  function spansToCharRanges(spans, start, end, kind) {
    var out = [];
    for (var i = 0; i < spans.length; i++) {
      var seg = spans[i][kind];
      if (seg[1] > start && seg[0] < end) out.push([i, i + 1]);
    }
    return mergeRanges(out);
  }

  // 高亮：把 ranges 合并后包 <mark class="ux-hl">，其余文本转义
  function highlight(text, ranges) {
    var s = String(text == null ? "" : text);
    var rs = mergeRanges(ranges || []);
    if (!rs.length) return escapeHtml(s);
    var out = "";
    var cursor = 0;
    for (var i = 0; i < rs.length; i++) {
      var a = clamp(rs[i][0], 0, s.length);
      var b = clamp(rs[i][1], a, s.length);
      out += escapeHtml(s.slice(cursor, a));
      out += '<mark class="ux-hl">' + escapeHtml(s.slice(a, b)) + "</mark>";
      cursor = b;
    }
    out += escapeHtml(s.slice(cursor));
    return out;
  }

  /* ============================================================
   * 3. 客户端搜索索引（几千条以内，浏览器内直接搜）
   * ============================================================ */

  // items: 对象数组；fields: 参与搜索的字段名数组（可按权重）
  // 返回 { query(q, limit) -> [{ item, score, field, ranges }] }
  function createSearch(items, fields, opts) {
    fields = fields || [];
    opts = opts || {};
    var limitFields = opts.maxFieldChars || 200;
    var data = (items || []).map(function (item, index) {
      var hay = fields.map(function (f) {
        if (typeof f !== "string") return "";
        var v = getPath(item, f);
        return v == null ? "" : String(v);
      });
      return { item: item, index: index, hay: hay, blob: normalize(hay.join(" \u0001 ")) };
    });

    function query(rawQuery, maxResults) {
      var q = normalize(rawQuery);
      if (!q) return [];
      var results = [];
      for (var i = 0; i < data.length; i++) {
        var row = data[i];
        // 先用整体 blob 过滤，降低多次字段计算
        var whole = fuzzyMatch(q, row.blob.slice(0, limitFields * fields.length));
        if (whole.score <= 0) continue;
        // 找到最佳字段用于展示高亮
        var bestField = 0;
        var bestFieldScore = -1;
        var bestRanges = [];
        for (var fi = 0; fi < row.hay.length; fi++) {
          var m = fuzzyMatch(q, row.hay[fi].slice(0, limitFields));
          if (m.score > bestFieldScore) {
            bestFieldScore = m.score;
            bestField = fi;
            bestRanges = m.ranges;
          }
        }
        var fieldName = typeof fields[bestField] === "string" ? fields[bestField] : "";
        results.push({
          item: row.item,
          index: row.index,
          score: whole.score * 0.5 + bestFieldScore * 0.5,
          field: fieldName,
          fieldIndex: bestField,
          ranges: bestRanges
        });
      }
      results.sort(function (a, b) { return b.score - a.score || a.index - b.index; });
      return maxResults ? results.slice(0, maxResults) : results;
    }

    return { query: query, size: data.length };
  }

  /* ============================================================
   * 4. 安全 DOM 工具
   * ============================================================ */

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) {
      if (k === "class") node.className = attrs[k];
      else if (k === "text") node.textContent = attrs[k];
      else if (k === "html") node.innerHTML = attrs[k];
      else if (k === "style") node.setAttribute("style", attrs[k]);
      else if (k.slice(0, 2) === "on" && typeof attrs[k] === "function") {
        node.addEventListener(k.slice(2), attrs[k]);
      } else if (attrs[k] != null) node.setAttribute(k, attrs[k]);
    });
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }

  var isTouch = (function () {
    try {
      // 以「粗指针」为准排除手机/平板触控设备；无 matchMedia 时回退到 ontouchstart
      if (window.matchMedia) return !window.matchMedia("(pointer: fine)").matches;
      return "ontouchstart" in window || navigator.maxTouchPoints > 0;
    } catch (e) { return false; }
  })();

  /* ============================================================
   * 5. 搜索建议：输入下方弹最多 6 条，上下键 + 回车 + 0.3s 防抖 + 高亮
   * ============================================================ */

  function attachSuggest(input, config) {
    config = config || {};
    var limit = config.limit || 6;
    var wait = config.debounce || 300;
    var search = config.search; // { query(q, n) } 或 function(q, n)
    var render = config.render || function (item) { return String(item.item[config.labelField || "label"] || ""); };
    var onSelect = config.onSelect || function () {};
    var activeIndex = -1;
    var current = [];

    var panel = el("div", { class: "ux-suggest", role: "listbox", hidden: "hidden" });
    input.parentNode.insertBefore(panel, input.nextSibling);
    input.setAttribute("autocomplete", "off");
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");

    // 面板定位：fixed + 输入框实测矩形。
    // 不依赖宿主父元素的定位上下文——PMS2 各页面的输入框父级多为 flex/static，
    // 绝对定位会把下拉面板甩到页面左上角。
    function place() {
      var r = input.getBoundingClientRect();
      if (!r || !r.width) return;
      var vh = window.innerHeight || 0;
      var h = panel.offsetHeight || 0;
      var top = r.bottom + 4;
      if (h && vh && top + h > vh && r.top - h - 4 > 0) top = r.top - h - 4; // 空间不足上翻
      panel.style.position = "fixed";
      panel.style.left = Math.max(0, r.left) + "px";
      panel.style.width = Math.max(120, r.width) + "px";
      panel.style.top = top + "px";
      panel.style.right = "auto";
    }

    var reposition = function () { if (!panel.hidden) place(); };
    window.addEventListener("scroll", reposition, true);
    window.addEventListener("resize", reposition);

    function doQuery() {
      var q = input.value;
      if (!q.trim()) { hide(); return; }
      var run = typeof search === "function" ? search : search.query;
      var results = run.call(search, q, limit) || [];
      current = results;
      activeIndex = -1;
      renderPanel();
    }
    var debounced = debounce(doQuery, wait);
    input.addEventListener("input", debounced);

    function renderPanel() {
      panel.innerHTML = "";
      if (!current.length) { hide(); return; }
      current.forEach(function (r, i) {
        // 约定：render(item, result) 应返回「命中字段」的值，
        // 这样 result.ranges（相对命中字段的区间）才能正确高亮。
        var raw = render(r.item, r);
        var safe = highlight(raw, r.ranges);
        var opt = el("div", {
          class: "ux-suggest-item" + (i === activeIndex ? " is-active" : ""),
          role: "option",
          "aria-selected": i === activeIndex ? "true" : "false"
        });
        opt.innerHTML = safe;
        opt.addEventListener("mousedown", function (e) {
          e.preventDefault();
          choose(i);
        });
        panel.appendChild(opt);
      });
      panel.hidden = false;
      input.setAttribute("aria-expanded", "true");
      place();
    }

    function hide() {
      panel.hidden = true;
      panel.innerHTML = "";
      current = [];
      activeIndex = -1;
      input.setAttribute("aria-expanded", "false");
    }

    function choose(i) {
      var r = current[i];
      if (!r) return;
      var raw = render(r.item, r);
      input.value = typeof raw === "string" ? raw.replace(/<[^>]*>/g, "") : String(raw);
      hide();
      onSelect(r.item, r);
    }

    input.addEventListener("keydown", function (e) {
      if (panel.hidden) return;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        activeIndex = (activeIndex + 1) % current.length;
        renderPanel();
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        activeIndex = (activeIndex - 1 + current.length) % current.length;
        renderPanel();
      } else if (e.key === "Enter") {
        if (activeIndex >= 0) { e.preventDefault(); choose(activeIndex); }
      } else if (e.key === "Escape") {
        hide();
      }
    });

    document.addEventListener("click", function (e) {
      if (!panel.contains(e.target) && e.target !== input) hide();
    });

    return { hide: hide, query: doQuery, destroy: function () { debounced.cancel(); panel.remove(); } };
  }

  /* ============================================================
   * 6. URL 状态同步：筛选 / 排序 / 页码 / 标签 → query 参数
   *    刷新保持、链接可分享、前进后退（popstate）可用
   * ============================================================ */

  function attachUrlState(root, config) {
    config = config || {};
    var scope = root || document;
    var controlled = scope.querySelectorAll("[data-ux-param]");
    var suppress = false;

    function keyOf(node) { return node.getAttribute("data-ux-param"); }

    function readFromUrl() {
      var params = new URLSearchParams(window.location.search);
      suppress = true;
      controlled.forEach(function (node) {
        var key = keyOf(node);
        if (!key) return;
        var val = params.get(key);
        if (node.type === "checkbox") {
          node.checked = val != null && val !== "" && val !== "0" && val !== "false";
        } else if (node.tagName === "SELECT" || node.type === "radio") {
          if (val != null) node.value = val;
        } else {
          node.value = val != null ? val : "";
        }
      });
      suppress = false;
      if (config.onRestore) config.onRestore(params);
    }

    function writeToUrl() {
      if (suppress) return;
      var url = new URL(window.location.href);
      controlled.forEach(function (node) {
        var key = keyOf(node);
        if (!key) return;
        var val = node.type === "checkbox"
          ? (node.checked ? "1" : "")
          : (node.value || "");
        if (val === "") url.searchParams.delete(key);
        else url.searchParams.set(key, val);
      });
      // 页码变动时回到顶部（符合用户预期），标签切换保留
      window.history.pushState({ ux: true }, "", url.pathname + url.search + url.hash);
    }

    controlled.forEach(function (node) {
      var evt = node.tagName === "SELECT" || node.type === "checkbox" || node.type === "radio" ? "change" : "input";
      node.addEventListener(evt, debounce(writeToUrl, config.debounce || 200));
    });

    window.addEventListener("popstate", readFromUrl);
    readFromUrl();
    return { read: readFromUrl, write: writeToUrl };
  }

  /* ============================================================
   * 7. 滚动与已加载内容恢复（详情页返回列表）
   * ============================================================ */

  var scrollMemory = (function () {
    var KEY = "pms-ux:scroll:";
    function save(key, extra) {
      try {
        sessionStorage.setItem(KEY + key, JSON.stringify({
          y: window.scrollY || window.pageYOffset || 0,
          extra: extra || null,
          at: Date.now()
        }));
      } catch (e) { /* 隐私模式忽略 */ }
    }
    function restore(key) {
      try {
        var raw = sessionStorage.getItem(KEY + key);
        if (!raw) return null;
        return JSON.parse(raw);
      } catch (e) { return null; }
    }
    function clear(key) {
      try { sessionStorage.removeItem(KEY + key); } catch (e) {}
    }
    return { save: save, restore: restore, clear: clear };
  })();

  function attachScrollRestore(config) {
    config = config || {};
    var key = config.key || window.location.pathname;
    var stored = scrollMemory.restore(key);
    if (stored) {
      if (config.onRestoreExtra && stored.extra) config.onRestoreExtra(stored.extra);
      // 双 rAF 等待布局稳定后再恢复，避免内容未渲染导致回顶
      requestAnimationFrame(function () {
        requestAnimationFrame(function () {
          window.scrollTo(0, stored.y || 0);
        });
      });
    }
    // 离开页面前保存（含可自定义的已加载内容状态）
    window.addEventListener("pagehide", function () {
      var extra = config.collectExtra ? config.collectExtra() : null;
      scrollMemory.save(key, extra);
    });
    return scrollMemory;
  }

  /* ============================================================
   * 8. 图片懒加载 + 占位 + 宽高比（防布局跳动）
   * ============================================================ */

  function enhanceImage(img) {
    if (img.dataset.uxImgDone === "1") return;
    img.dataset.uxImgDone = "1";
    // 预设宽高比：优先 data-ux-w/h，其次 width/height 属性
    var w = img.getAttribute("data-ux-w") || img.getAttribute("width");
    var h = img.getAttribute("data-ux-h") || img.getAttribute("height");
    if (w && h && !img.style.aspectRatio) {
      img.style.aspectRatio = w + " / " + h;
    }
    // 占位底色
    if (!img.style.background) img.style.background = "var(--color-bg-subtle, #f5f7fa)";
    if (!img.style.objectFit) img.style.objectFit = "cover";
    var rect = img.getBoundingClientRect();
    var belowFold = rect.top > (window.innerHeight || 800);
    if (belowFold && "loading" in img) {
      img.setAttribute("loading", "lazy");
      img.setAttribute("decoding", "async");
    }
  }

  function attachImageLazy(root) {
    var scope = root || document;
    var imgs = scope.querySelectorAll("img");
    Array.prototype.forEach.call(imgs, enhanceImage);
    // 动态渲染内容：观察新增 img
    if (typeof MutationObserver !== "undefined") {
      var mo = new MutationObserver(function (records) {
        records.forEach(function (rec) {
          Array.prototype.forEach.call(rec.addedNodes || [], function (node) {
            if (node.nodeType !== 1) return;
            if (node.tagName === "IMG") enhanceImage(node);
            Array.prototype.forEach.call(node.querySelectorAll ? node.querySelectorAll("img") : [], enhanceImage);
          });
        });
      });
      mo.observe(scope === document ? document.body : scope, { childList: true, subtree: true });
    }
  }

  /* ============================================================
   * 9. 表单守卫：未保存提醒 / 草稿 / 实时校验 / 提交防重复 / 键盘
   * ============================================================ */

  function attachFormGuard(form, config) {
    config = config || {};
    var useDraft = config.draft === true || form.hasAttribute("data-ux-draft");
    var draftKey = "pms-ux:draft:" + (config.draftKey || form.getAttribute("data-ux-draft-key") || form.id || window.location.pathname);
    var savedFlag = el("span", { class: "ux-saved", text: "", role: "status", "aria-live": "polite" });
    savedFlag.hidden = true;

    function serialize() {
      var out = {};
      Array.prototype.forEach.call(form.querySelectorAll("input,textarea,select"), function (field) {
        if (!field.name || field.type === "password" || field.type === "hidden") return;
        if (field.type === "checkbox") out[field.name] = field.checked ? "1" : "";
        else out[field.name] = field.value;
      });
      return out;
    }
    function applyDraft(data) {
      if (!data) return;
      Array.prototype.forEach.call(form.querySelectorAll("input,textarea,select"), function (field) {
        if (!field.name || field.type === "password" || field.type === "hidden") return;
        if (!(field.name in data)) return;
        if (field.type === "checkbox") field.checked = data[field.name] === "1";
        else field.value = data[field.name];
      });
    }
    function clearDraft() {
      try { localStorage.removeItem(draftKey); } catch (e) {}
    }
    function saveDraft() {
      try {
        localStorage.setItem(draftKey, JSON.stringify({ data: serialize(), at: Date.now() }));
        savedFlag.textContent = "已保存";
        savedFlag.hidden = false;
      } catch (e) { /* 存储不可用时静默 */ }
    }
    var debouncedSave = debounce(saveDraft, 1000);

    if (useDraft) {
      try {
        var raw = localStorage.getItem(draftKey);
        if (raw) applyDraft(JSON.parse(raw).data);
      } catch (e) {}
      form.addEventListener("input", function () { savedFlag.textContent = "保存中…"; savedFlag.hidden = false; debouncedSave(); });
      form.addEventListener("change", debouncedSave);
      var flagHost = form.querySelector("[data-ux-saved-slot]") || form;
      flagHost.appendChild(savedFlag);
      // 注意：此处只取消待写入的防抖保存，草稿的清除必须等校验通过后再做
      // （见下方 submit 处理器），否则校验失败会连带丢掉用户已填内容。
      form.addEventListener("submit", function () { debouncedSave.cancel(); }, true);
    }

    // 未保存提醒：基于初始快照比较，保存成功后由 clearDirty() 解除
    var initial = JSON.stringify(serialize());
    var dirty = false;
    form.addEventListener("input", function () { dirty = true; });
    form.addEventListener("change", function () { dirty = true; });
    function beforeUnload(e) {
      if (!dirty) return;
      e.preventDefault();
      e.returnValue = "";
    }
    window.addEventListener("beforeunload", beforeUnload);
    form.__uxClearDirty = function () {
      dirty = false;
      initial = JSON.stringify(serialize());
    };
    /* 解除全局监听：抽屉等动态容器会反复创建/销毁表单，
       不解除则 beforeunload 监听器随操作次数累积。 */
    form.__uxDestroy = function () {
      window.removeEventListener("beforeunload", beforeUnload);
      if (useDraft) debouncedSave.cancel();
    };

    // 实时校验：失焦检查，错误显示在输入框下方，改对即消失
    if (config.validate !== false && form.hasAttribute("data-ux-validate")) {
      Array.prototype.forEach.call(form.querySelectorAll("[data-ux-rule]"), function (field) {
        var rule = field.getAttribute("data-ux-rule"); // required | phone | code | email | number
        var err = el("div", { class: "ux-field-error", role: "alert", hidden: "hidden" });
        field.parentNode.insertBefore(err, field.nextSibling);
        function check() {
          var v = String(field.value || "").trim();
          var msg = "";
          if (rule === "required" && !v) msg = "此项为必填项";
          else if (rule === "phone" && !/^1\d{10}$/.test(v)) msg = "请输入 11 位手机号";
          else if (rule === "code" && !/^\d{4,8}$/.test(v)) msg = "请输入 4-8 位数字验证码";
          else if (rule === "email" && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) msg = "请输入有效的邮箱地址";
          else if (rule === "number" && v && !/^\d+$/.test(v)) msg = "请输入数字";
          if (msg) { err.textContent = msg; err.hidden = false; field.setAttribute("aria-invalid", "true"); }
          else { err.textContent = ""; err.hidden = true; field.removeAttribute("aria-invalid"); }
          return !msg;
        }
        field.addEventListener("blur", check);
        field.addEventListener("input", function () { if (!err.hidden) check(); });
        field.__uxCheck = check;
      });
    }

    // 提交：校验失败聚焦第一个错误框；按钮 loading + 防重复
    form.addEventListener("submit", function (e) {
      var checkers = form.querySelectorAll("[data-ux-rule]");
      var firstBad = null;
      Array.prototype.forEach.call(checkers, function (field) {
        if (field.__uxCheck && !field.__uxCheck() && !firstBad) firstBad = field;
      });
      if (firstBad) {
        e.preventDefault();
        try { firstBad.focus(); } catch (err) { firstBad.focus(); }
        firstBad.scrollIntoView({ block: "center" });
        return;
      }
      // 校验通过：清除草稿（提交失败仍可在页面上保留用户输入）
      if (useDraft) clearDraft();
      // 防重复：由 data-ux-submit 标记的按钮承载
      var btn = form.querySelector("[data-ux-submit]");
      if (btn) {
        if (btn.disabled) { e.preventDefault(); return; }
        btn.disabled = true;
        btn.classList.add("is-loading");
        // 表单为原生 POST 时无需手动恢复；AJAX 场景由调用方调用 release()
        form.__uxRelease = function () { btn.disabled = false; btn.classList.remove("is-loading"); };
      }
    });
    return form;
  }

  /* ============================================================
   * 10. 全局低风险增强：自动聚焦 / Esc 关弹窗 / 输入提示 / 真链接
   * ============================================================ */

  function autofocusFirst() {
    if (isTouch) return; // 手机/触控设备不自动聚焦
    if (window.location.hash) return;
    // 只对声明式表单生效，避免扰动既有列表页/工作台的焦点与键盘交互
    var form = document.querySelector("form[data-ux-form][data-ux-autofocus]");
    if (!form) return;
    var target = form.querySelector("input:not([type=hidden]):not([disabled]):not([readonly])");
    if (target) { try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); } }
  }

  function attachEscClose() {
    if (attachEscClose.__bound) return; // 幂等：避免重复绑定导致 Esc 被处理多次
    attachEscClose.__bound = true;
    document.addEventListener("keydown", function (e) {
      if (e.key !== "Escape") return;
      var modal = document.querySelector("[data-ux-modal]:not([hidden])");
      if (modal && modal.__uxClose) { e.preventDefault(); modal.__uxClose(); }
    });
    // 约定：data-ux-close 按钮关闭最近的 [data-ux-modal]
    document.addEventListener("click", function (e) {
      var btn = e.target.closest ? e.target.closest("[data-ux-close]") : null;
      if (!btn) return;
      var modal = btn.closest("[data-ux-modal]");
      if (modal && modal.__uxClose) modal.__uxClose();
    });
  }

  function registerModal(modal, closeFn) {
    modal.__uxClose = closeFn;
    return modal;
  }

  function applyInputHints(root) {
    var scope = root || document;
    Array.prototype.forEach.call(scope.querySelectorAll("input"), function (input) {
      if (input.hasAttribute("data-ux-hint-done")) return;
      var name = (input.name || input.id || input.placeholder || "").toLowerCase();
      var type = (input.type || "").toLowerCase();
      if (/phone|mobile|tel|手机/.test(name) && !input.getAttribute("inputmode")) {
        input.setAttribute("inputmode", "numeric");
        if (!input.getAttribute("autocomplete")) input.setAttribute("autocomplete", "tel");
      } else if (/code|otp|verify|验证码/.test(name) && !input.getAttribute("inputmode")) {
        input.setAttribute("inputmode", "numeric");
        if (!input.getAttribute("autocomplete")) input.setAttribute("autocomplete", "one-time-code");
      } else if (/email|邮箱/.test(name) && !input.getAttribute("inputmode")) {
        input.setAttribute("inputmode", "email");
        if (!input.getAttribute("autocomplete")) input.setAttribute("autocomplete", "email");
      }
      input.setAttribute("data-ux-hint-done", "1");
    });
  }

  // 真链接增强：把带 data-ux-href 的非 a 元素替换为 a（保留样式与子元素），
  // 使其天然支持右键新开、Ctrl/中键点击。不改变已有 <a>。
  function enhanceLinks(root) {
    var scope = root || document;
    var candidates = scope.querySelectorAll("[data-ux-href]:not(a)");
    Array.prototype.forEach.call(candidates, function (node) {
      var href = node.getAttribute("data-ux-href");
      if (!href) return;
      var a = el("a", { href: href, class: node.className || "", "data-ux-link": "1" });
      while (node.firstChild) a.appendChild(node.firstChild);
      Array.prototype.forEach.call(node.attributes, function (attr) {
        if (attr.name !== "data-ux-href" && attr.name !== "class") a.setAttribute(attr.name, attr.value);
      });
      node.parentNode.replaceChild(a, node);
    });
  }

  /* ============================================================
   * 11. 声明式自动引导（扫描 data-ux-*）
   * ============================================================ */

  function autoBoot() {
    // 表单守卫
    Array.prototype.forEach.call(document.querySelectorAll("form[data-ux-form]"), function (form) {
      attachFormGuard(form, {});
    });
    // URL 状态：容器声明 data-ux-urlstate
    Array.prototype.forEach.call(document.querySelectorAll("[data-ux-urlstate]"), function (root) {
      attachUrlState(root, {});
    });
    // 图片 / 输入提示 / 键盘 / 聚焦
    attachImageLazy(document);
    applyInputHints(document);
    attachEscClose();
    autofocusFirst();
  }

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", autoBoot);
    } else {
      autoBoot();
    }
  }

  /* ============================================================
   * 12. 导出
   * ============================================================ */

  return {
    version: "1.0.0",
    debounce: debounce,
    escapeHtml: escapeHtml,
    normalize: normalize,
    getPath: getPath,
    levenshtein: levenshtein,
    fuzzyMatch: fuzzyMatch,
    highlight: highlight,
    pinyinize: pinyinize,
    pinyinOfChar: pinyinOfChar,
    registerPinyin: registerPinyin,
    createSearch: createSearch,
    attachSuggest: attachSuggest,
    attachUrlState: attachUrlState,
    attachScrollRestore: attachScrollRestore,
    scrollMemory: scrollMemory,
    attachImageLazy: attachImageLazy,
    enhanceImage: enhanceImage,
    attachFormGuard: attachFormGuard,
    applyInputHints: applyInputHints,
    enhanceLinks: enhanceLinks,
    attachEscClose: attachEscClose,
    registerModal: registerModal,
    autofocusFirst: autofocusFirst,
    autoBoot: autoBoot,
    isTouch: isTouch
  };
});
