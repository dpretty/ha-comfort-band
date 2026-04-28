const Z = globalThis, rt = Z.ShadowRoot && (Z.ShadyCSS === void 0 || Z.ShadyCSS.nativeShadow) && "adoptedStyleSheets" in Document.prototype && "replace" in CSSStyleSheet.prototype, st = Symbol(), pt = /* @__PURE__ */ new WeakMap();
let xt = class {
  constructor(t, e, r) {
    if (this._$cssResult$ = !0, r !== st) throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");
    this.cssText = t, this.t = e;
  }
  get styleSheet() {
    let t = this.o;
    const e = this.t;
    if (rt && t === void 0) {
      const r = e !== void 0 && e.length === 1;
      r && (t = pt.get(e)), t === void 0 && ((this.o = t = new CSSStyleSheet()).replaceSync(this.cssText), r && pt.set(e, t));
    }
    return t;
  }
  toString() {
    return this.cssText;
  }
};
const Ht = (i) => new xt(typeof i == "string" ? i : i + "", void 0, st), O = (i, ...t) => {
  const e = i.length === 1 ? i[0] : t.reduce((r, s, o) => r + ((n) => {
    if (n._$cssResult$ === !0) return n.cssText;
    if (typeof n == "number") return n;
    throw Error("Value passed to 'css' function must be a 'css' function result: " + n + ". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.");
  })(s) + i[o + 1], i[0]);
  return new xt(e, i, st);
}, kt = (i, t) => {
  if (rt) i.adoptedStyleSheets = t.map((e) => e instanceof CSSStyleSheet ? e : e.styleSheet);
  else for (const e of t) {
    const r = document.createElement("style"), s = Z.litNonce;
    s !== void 0 && r.setAttribute("nonce", s), r.textContent = e.cssText, i.appendChild(r);
  }
}, ut = rt ? (i) => i : (i) => i instanceof CSSStyleSheet ? ((t) => {
  let e = "";
  for (const r of t.cssRules) e += r.cssText;
  return Ht(e);
})(i) : i;
const { is: Mt, defineProperty: Dt, getOwnPropertyDescriptor: Ut, getOwnPropertyNames: Rt, getOwnPropertySymbols: Lt, getPrototypeOf: jt } = Object, J = globalThis, ft = J.trustedTypes, Bt = ft ? ft.emptyScript : "", It = J.reactiveElementPolyfillSupport, j = (i, t) => i, G = { toAttribute(i, t) {
  switch (t) {
    case Boolean:
      i = i ? Bt : null;
      break;
    case Object:
    case Array:
      i = i == null ? i : JSON.stringify(i);
  }
  return i;
}, fromAttribute(i, t) {
  let e = i;
  switch (t) {
    case Boolean:
      e = i !== null;
      break;
    case Number:
      e = i === null ? null : Number(i);
      break;
    case Object:
    case Array:
      try {
        e = JSON.parse(i);
      } catch {
        e = null;
      }
  }
  return e;
} }, ot = (i, t) => !Mt(i, t), mt = { attribute: !0, type: String, converter: G, reflect: !1, useDefault: !1, hasChanged: ot };
Symbol.metadata ??= Symbol("metadata"), J.litPropertyMetadata ??= /* @__PURE__ */ new WeakMap();
let T = class extends HTMLElement {
  static addInitializer(t) {
    this._$Ei(), (this.l ??= []).push(t);
  }
  static get observedAttributes() {
    return this.finalize(), this._$Eh && [...this._$Eh.keys()];
  }
  static createProperty(t, e = mt) {
    if (e.state && (e.attribute = !1), this._$Ei(), this.prototype.hasOwnProperty(t) && ((e = Object.create(e)).wrapped = !0), this.elementProperties.set(t, e), !e.noAccessor) {
      const r = Symbol(), s = this.getPropertyDescriptor(t, r, e);
      s !== void 0 && Dt(this.prototype, t, s);
    }
  }
  static getPropertyDescriptor(t, e, r) {
    const { get: s, set: o } = Ut(this.prototype, t) ?? { get() {
      return this[e];
    }, set(n) {
      this[e] = n;
    } };
    return { get: s, set(n) {
      const c = s?.call(this);
      o?.call(this, n), this.requestUpdate(t, c, r);
    }, configurable: !0, enumerable: !0 };
  }
  static getPropertyOptions(t) {
    return this.elementProperties.get(t) ?? mt;
  }
  static _$Ei() {
    if (this.hasOwnProperty(j("elementProperties"))) return;
    const t = jt(this);
    t.finalize(), t.l !== void 0 && (this.l = [...t.l]), this.elementProperties = new Map(t.elementProperties);
  }
  static finalize() {
    if (this.hasOwnProperty(j("finalized"))) return;
    if (this.finalized = !0, this._$Ei(), this.hasOwnProperty(j("properties"))) {
      const e = this.properties, r = [...Rt(e), ...Lt(e)];
      for (const s of r) this.createProperty(s, e[s]);
    }
    const t = this[Symbol.metadata];
    if (t !== null) {
      const e = litPropertyMetadata.get(t);
      if (e !== void 0) for (const [r, s] of e) this.elementProperties.set(r, s);
    }
    this._$Eh = /* @__PURE__ */ new Map();
    for (const [e, r] of this.elementProperties) {
      const s = this._$Eu(e, r);
      s !== void 0 && this._$Eh.set(s, e);
    }
    this.elementStyles = this.finalizeStyles(this.styles);
  }
  static finalizeStyles(t) {
    const e = [];
    if (Array.isArray(t)) {
      const r = new Set(t.flat(1 / 0).reverse());
      for (const s of r) e.unshift(ut(s));
    } else t !== void 0 && e.push(ut(t));
    return e;
  }
  static _$Eu(t, e) {
    const r = e.attribute;
    return r === !1 ? void 0 : typeof r == "string" ? r : typeof t == "string" ? t.toLowerCase() : void 0;
  }
  constructor() {
    super(), this._$Ep = void 0, this.isUpdatePending = !1, this.hasUpdated = !1, this._$Em = null, this._$Ev();
  }
  _$Ev() {
    this._$ES = new Promise((t) => this.enableUpdating = t), this._$AL = /* @__PURE__ */ new Map(), this._$E_(), this.requestUpdate(), this.constructor.l?.forEach((t) => t(this));
  }
  addController(t) {
    (this._$EO ??= /* @__PURE__ */ new Set()).add(t), this.renderRoot !== void 0 && this.isConnected && t.hostConnected?.();
  }
  removeController(t) {
    this._$EO?.delete(t);
  }
  _$E_() {
    const t = /* @__PURE__ */ new Map(), e = this.constructor.elementProperties;
    for (const r of e.keys()) this.hasOwnProperty(r) && (t.set(r, this[r]), delete this[r]);
    t.size > 0 && (this._$Ep = t);
  }
  createRenderRoot() {
    const t = this.shadowRoot ?? this.attachShadow(this.constructor.shadowRootOptions);
    return kt(t, this.constructor.elementStyles), t;
  }
  connectedCallback() {
    this.renderRoot ??= this.createRenderRoot(), this.enableUpdating(!0), this._$EO?.forEach((t) => t.hostConnected?.());
  }
  enableUpdating(t) {
  }
  disconnectedCallback() {
    this._$EO?.forEach((t) => t.hostDisconnected?.());
  }
  attributeChangedCallback(t, e, r) {
    this._$AK(t, r);
  }
  _$ET(t, e) {
    const r = this.constructor.elementProperties.get(t), s = this.constructor._$Eu(t, r);
    if (s !== void 0 && r.reflect === !0) {
      const o = (r.converter?.toAttribute !== void 0 ? r.converter : G).toAttribute(e, r.type);
      this._$Em = t, o == null ? this.removeAttribute(s) : this.setAttribute(s, o), this._$Em = null;
    }
  }
  _$AK(t, e) {
    const r = this.constructor, s = r._$Eh.get(t);
    if (s !== void 0 && this._$Em !== s) {
      const o = r.getPropertyOptions(s), n = typeof o.converter == "function" ? { fromAttribute: o.converter } : o.converter?.fromAttribute !== void 0 ? o.converter : G;
      this._$Em = s;
      const c = n.fromAttribute(e, o.type);
      this[s] = c ?? this._$Ej?.get(s) ?? c, this._$Em = null;
    }
  }
  requestUpdate(t, e, r, s = !1, o) {
    if (t !== void 0) {
      const n = this.constructor;
      if (s === !1 && (o = this[t]), r ??= n.getPropertyOptions(t), !((r.hasChanged ?? ot)(o, e) || r.useDefault && r.reflect && o === this._$Ej?.get(t) && !this.hasAttribute(n._$Eu(t, r)))) return;
      this.C(t, e, r);
    }
    this.isUpdatePending === !1 && (this._$ES = this._$EP());
  }
  C(t, e, { useDefault: r, reflect: s, wrapped: o }, n) {
    r && !(this._$Ej ??= /* @__PURE__ */ new Map()).has(t) && (this._$Ej.set(t, n ?? e ?? this[t]), o !== !0 || n !== void 0) || (this._$AL.has(t) || (this.hasUpdated || r || (e = void 0), this._$AL.set(t, e)), s === !0 && this._$Em !== t && (this._$Eq ??= /* @__PURE__ */ new Set()).add(t));
  }
  async _$EP() {
    this.isUpdatePending = !0;
    try {
      await this._$ES;
    } catch (e) {
      Promise.reject(e);
    }
    const t = this.scheduleUpdate();
    return t != null && await t, !this.isUpdatePending;
  }
  scheduleUpdate() {
    return this.performUpdate();
  }
  performUpdate() {
    if (!this.isUpdatePending) return;
    if (!this.hasUpdated) {
      if (this.renderRoot ??= this.createRenderRoot(), this._$Ep) {
        for (const [s, o] of this._$Ep) this[s] = o;
        this._$Ep = void 0;
      }
      const r = this.constructor.elementProperties;
      if (r.size > 0) for (const [s, o] of r) {
        const { wrapped: n } = o, c = this[s];
        n !== !0 || this._$AL.has(s) || c === void 0 || this.C(s, void 0, o, c);
      }
    }
    let t = !1;
    const e = this._$AL;
    try {
      t = this.shouldUpdate(e), t ? (this.willUpdate(e), this._$EO?.forEach((r) => r.hostUpdate?.()), this.update(e)) : this._$EM();
    } catch (r) {
      throw t = !1, this._$EM(), r;
    }
    t && this._$AE(e);
  }
  willUpdate(t) {
  }
  _$AE(t) {
    this._$EO?.forEach((e) => e.hostUpdated?.()), this.hasUpdated || (this.hasUpdated = !0, this.firstUpdated(t)), this.updated(t);
  }
  _$EM() {
    this._$AL = /* @__PURE__ */ new Map(), this.isUpdatePending = !1;
  }
  get updateComplete() {
    return this.getUpdateComplete();
  }
  getUpdateComplete() {
    return this._$ES;
  }
  shouldUpdate(t) {
    return !0;
  }
  update(t) {
    this._$Eq &&= this._$Eq.forEach((e) => this._$ET(e, this[e])), this._$EM();
  }
  updated(t) {
  }
  firstUpdated(t) {
  }
};
T.elementStyles = [], T.shadowRootOptions = { mode: "open" }, T[j("elementProperties")] = /* @__PURE__ */ new Map(), T[j("finalized")] = /* @__PURE__ */ new Map(), It?.({ ReactiveElement: T }), (J.reactiveElementVersions ??= []).push("2.1.2");
const nt = globalThis, vt = (i) => i, X = nt.trustedTypes, gt = X ? X.createPolicy("lit-html", { createHTML: (i) => i }) : void 0, At = "$lit$", x = `lit$${Math.random().toFixed(9).slice(2)}$`, Et = "?" + x, Ft = `<${Et}>`, C = document, B = () => C.createComment(""), I = (i) => i === null || typeof i != "object" && typeof i != "function", at = Array.isArray, Vt = (i) => at(i) || typeof i?.[Symbol.iterator] == "function", tt = `[ 	
\f\r]`, L = /<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g, bt = /-->/g, _t = />/g, S = RegExp(`>|${tt}(?:([^\\s"'>=/]+)(${tt}*=${tt}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`, "g"), $t = /'/g, yt = /"/g, St = /^(?:script|style|textarea|title)$/i, Nt = (i) => (t, ...e) => ({ _$litType$: i, strings: t, values: e }), f = Nt(1), W = Nt(2), z = Symbol.for("lit-noChange"), u = Symbol.for("lit-nothing"), wt = /* @__PURE__ */ new WeakMap(), N = C.createTreeWalker(C, 129);
function Ct(i, t) {
  if (!at(i) || !i.hasOwnProperty("raw")) throw Error("invalid template strings array");
  return gt !== void 0 ? gt.createHTML(t) : t;
}
const qt = (i, t) => {
  const e = i.length - 1, r = [];
  let s, o = t === 2 ? "<svg>" : t === 3 ? "<math>" : "", n = L;
  for (let c = 0; c < e; c++) {
    const a = i[c];
    let p, d, l = -1, m = 0;
    for (; m < a.length && (n.lastIndex = m, d = n.exec(a), d !== null); ) m = n.lastIndex, n === L ? d[1] === "!--" ? n = bt : d[1] !== void 0 ? n = _t : d[2] !== void 0 ? (St.test(d[2]) && (s = RegExp("</" + d[2], "g")), n = S) : d[3] !== void 0 && (n = S) : n === S ? d[0] === ">" ? (n = s ?? L, l = -1) : d[1] === void 0 ? l = -2 : (l = n.lastIndex - d[2].length, p = d[1], n = d[3] === void 0 ? S : d[3] === '"' ? yt : $t) : n === yt || n === $t ? n = S : n === bt || n === _t ? n = L : (n = S, s = void 0);
    const w = n === S && i[c + 1].startsWith("/>") ? " " : "";
    o += n === L ? a + Ft : l >= 0 ? (r.push(p), a.slice(0, l) + At + a.slice(l) + x + w) : a + x + (l === -2 ? c : w);
  }
  return [Ct(i, o + (i[e] || "<?>") + (t === 2 ? "</svg>" : t === 3 ? "</math>" : "")), r];
};
class F {
  constructor({ strings: t, _$litType$: e }, r) {
    let s;
    this.parts = [];
    let o = 0, n = 0;
    const c = t.length - 1, a = this.parts, [p, d] = qt(t, e);
    if (this.el = F.createElement(p, r), N.currentNode = this.el.content, e === 2 || e === 3) {
      const l = this.el.content.firstChild;
      l.replaceWith(...l.childNodes);
    }
    for (; (s = N.nextNode()) !== null && a.length < c; ) {
      if (s.nodeType === 1) {
        if (s.hasAttributes()) for (const l of s.getAttributeNames()) if (l.endsWith(At)) {
          const m = d[n++], w = s.getAttribute(l).split(x), K = /([.?@])?(.*)/.exec(m);
          a.push({ type: 1, index: o, name: K[2], strings: w, ctor: K[1] === "." ? Wt : K[1] === "?" ? Zt : K[1] === "@" ? Gt : Y }), s.removeAttribute(l);
        } else l.startsWith(x) && (a.push({ type: 6, index: o }), s.removeAttribute(l));
        if (St.test(s.tagName)) {
          const l = s.textContent.split(x), m = l.length - 1;
          if (m > 0) {
            s.textContent = X ? X.emptyScript : "";
            for (let w = 0; w < m; w++) s.append(l[w], B()), N.nextNode(), a.push({ type: 2, index: ++o });
            s.append(l[m], B());
          }
        }
      } else if (s.nodeType === 8) if (s.data === Et) a.push({ type: 2, index: o });
      else {
        let l = -1;
        for (; (l = s.data.indexOf(x, l + 1)) !== -1; ) a.push({ type: 7, index: o }), l += x.length - 1;
      }
      o++;
    }
  }
  static createElement(t, e) {
    const r = C.createElement("template");
    return r.innerHTML = t, r;
  }
}
function H(i, t, e = i, r) {
  if (t === z) return t;
  let s = r !== void 0 ? e._$Co?.[r] : e._$Cl;
  const o = I(t) ? void 0 : t._$litDirective$;
  return s?.constructor !== o && (s?._$AO?.(!1), o === void 0 ? s = void 0 : (s = new o(i), s._$AT(i, e, r)), r !== void 0 ? (e._$Co ??= [])[r] = s : e._$Cl = s), s !== void 0 && (t = H(i, s._$AS(i, t.values), s, r)), t;
}
class Kt {
  constructor(t, e) {
    this._$AV = [], this._$AN = void 0, this._$AD = t, this._$AM = e;
  }
  get parentNode() {
    return this._$AM.parentNode;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  u(t) {
    const { el: { content: e }, parts: r } = this._$AD, s = (t?.creationScope ?? C).importNode(e, !0);
    N.currentNode = s;
    let o = N.nextNode(), n = 0, c = 0, a = r[0];
    for (; a !== void 0; ) {
      if (n === a.index) {
        let p;
        a.type === 2 ? p = new V(o, o.nextSibling, this, t) : a.type === 1 ? p = new a.ctor(o, a.name, a.strings, this, t) : a.type === 6 && (p = new Xt(o, this, t)), this._$AV.push(p), a = r[++c];
      }
      n !== a?.index && (o = N.nextNode(), n++);
    }
    return N.currentNode = C, s;
  }
  p(t) {
    let e = 0;
    for (const r of this._$AV) r !== void 0 && (r.strings !== void 0 ? (r._$AI(t, r, e), e += r.strings.length - 2) : r._$AI(t[e])), e++;
  }
}
class V {
  get _$AU() {
    return this._$AM?._$AU ?? this._$Cv;
  }
  constructor(t, e, r, s) {
    this.type = 2, this._$AH = u, this._$AN = void 0, this._$AA = t, this._$AB = e, this._$AM = r, this.options = s, this._$Cv = s?.isConnected ?? !0;
  }
  get parentNode() {
    let t = this._$AA.parentNode;
    const e = this._$AM;
    return e !== void 0 && t?.nodeType === 11 && (t = e.parentNode), t;
  }
  get startNode() {
    return this._$AA;
  }
  get endNode() {
    return this._$AB;
  }
  _$AI(t, e = this) {
    t = H(this, t, e), I(t) ? t === u || t == null || t === "" ? (this._$AH !== u && this._$AR(), this._$AH = u) : t !== this._$AH && t !== z && this._(t) : t._$litType$ !== void 0 ? this.$(t) : t.nodeType !== void 0 ? this.T(t) : Vt(t) ? this.k(t) : this._(t);
  }
  O(t) {
    return this._$AA.parentNode.insertBefore(t, this._$AB);
  }
  T(t) {
    this._$AH !== t && (this._$AR(), this._$AH = this.O(t));
  }
  _(t) {
    this._$AH !== u && I(this._$AH) ? this._$AA.nextSibling.data = t : this.T(C.createTextNode(t)), this._$AH = t;
  }
  $(t) {
    const { values: e, _$litType$: r } = t, s = typeof r == "number" ? this._$AC(t) : (r.el === void 0 && (r.el = F.createElement(Ct(r.h, r.h[0]), this.options)), r);
    if (this._$AH?._$AD === s) this._$AH.p(e);
    else {
      const o = new Kt(s, this), n = o.u(this.options);
      o.p(e), this.T(n), this._$AH = o;
    }
  }
  _$AC(t) {
    let e = wt.get(t.strings);
    return e === void 0 && wt.set(t.strings, e = new F(t)), e;
  }
  k(t) {
    at(this._$AH) || (this._$AH = [], this._$AR());
    const e = this._$AH;
    let r, s = 0;
    for (const o of t) s === e.length ? e.push(r = new V(this.O(B()), this.O(B()), this, this.options)) : r = e[s], r._$AI(o), s++;
    s < e.length && (this._$AR(r && r._$AB.nextSibling, s), e.length = s);
  }
  _$AR(t = this._$AA.nextSibling, e) {
    for (this._$AP?.(!1, !0, e); t !== this._$AB; ) {
      const r = vt(t).nextSibling;
      vt(t).remove(), t = r;
    }
  }
  setConnected(t) {
    this._$AM === void 0 && (this._$Cv = t, this._$AP?.(t));
  }
}
class Y {
  get tagName() {
    return this.element.tagName;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  constructor(t, e, r, s, o) {
    this.type = 1, this._$AH = u, this._$AN = void 0, this.element = t, this.name = e, this._$AM = s, this.options = o, r.length > 2 || r[0] !== "" || r[1] !== "" ? (this._$AH = Array(r.length - 1).fill(new String()), this.strings = r) : this._$AH = u;
  }
  _$AI(t, e = this, r, s) {
    const o = this.strings;
    let n = !1;
    if (o === void 0) t = H(this, t, e, 0), n = !I(t) || t !== this._$AH && t !== z, n && (this._$AH = t);
    else {
      const c = t;
      let a, p;
      for (t = o[0], a = 0; a < o.length - 1; a++) p = H(this, c[r + a], e, a), p === z && (p = this._$AH[a]), n ||= !I(p) || p !== this._$AH[a], p === u ? t = u : t !== u && (t += (p ?? "") + o[a + 1]), this._$AH[a] = p;
    }
    n && !s && this.j(t);
  }
  j(t) {
    t === u ? this.element.removeAttribute(this.name) : this.element.setAttribute(this.name, t ?? "");
  }
}
class Wt extends Y {
  constructor() {
    super(...arguments), this.type = 3;
  }
  j(t) {
    this.element[this.name] = t === u ? void 0 : t;
  }
}
class Zt extends Y {
  constructor() {
    super(...arguments), this.type = 4;
  }
  j(t) {
    this.element.toggleAttribute(this.name, !!t && t !== u);
  }
}
class Gt extends Y {
  constructor(t, e, r, s, o) {
    super(t, e, r, s, o), this.type = 5;
  }
  _$AI(t, e = this) {
    if ((t = H(this, t, e, 0) ?? u) === z) return;
    const r = this._$AH, s = t === u && r !== u || t.capture !== r.capture || t.once !== r.once || t.passive !== r.passive, o = t !== u && (r === u || s);
    s && this.element.removeEventListener(this.name, this, r), o && this.element.addEventListener(this.name, this, t), this._$AH = t;
  }
  handleEvent(t) {
    typeof this._$AH == "function" ? this._$AH.call(this.options?.host ?? this.element, t) : this._$AH.handleEvent(t);
  }
}
class Xt {
  constructor(t, e, r) {
    this.element = t, this.type = 6, this._$AN = void 0, this._$AM = e, this.options = r;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  _$AI(t) {
    H(this, t);
  }
}
const Jt = nt.litHtmlPolyfillSupport;
Jt?.(F, V), (nt.litHtmlVersions ??= []).push("3.3.2");
const Yt = (i, t, e) => {
  const r = e?.renderBefore ?? t;
  let s = r._$litPart$;
  if (s === void 0) {
    const o = e?.renderBefore ?? null;
    r._$litPart$ = s = new V(t.insertBefore(B(), o), o, void 0, e ?? {});
  }
  return s._$AI(i), s;
};
const ct = globalThis;
class b extends T {
  constructor() {
    super(...arguments), this.renderOptions = { host: this }, this._$Do = void 0;
  }
  createRenderRoot() {
    const t = super.createRenderRoot();
    return this.renderOptions.renderBefore ??= t.firstChild, t;
  }
  update(t) {
    const e = this.render();
    this.hasUpdated || (this.renderOptions.isConnected = this.isConnected), super.update(t), this._$Do = Yt(e, this.renderRoot, this.renderOptions);
  }
  connectedCallback() {
    super.connectedCallback(), this._$Do?.setConnected(!0);
  }
  disconnectedCallback() {
    super.disconnectedCallback(), this._$Do?.setConnected(!1);
  }
  render() {
    return z;
  }
}
b._$litElement$ = !0, b.finalized = !0, ct.litElementHydrateSupport?.({ LitElement: b });
const Qt = ct.litElementPolyfillSupport;
Qt?.({ LitElement: b });
(ct.litElementVersions ??= []).push("4.2.2");
const M = (i) => (t, e) => {
  e !== void 0 ? e.addInitializer(() => {
    customElements.define(i, t);
  }) : customElements.define(i, t);
};
const te = { attribute: !0, type: String, converter: G, reflect: !1, hasChanged: ot }, ee = (i = te, t, e) => {
  const { kind: r, metadata: s } = e;
  let o = globalThis.litPropertyMetadata.get(s);
  if (o === void 0 && globalThis.litPropertyMetadata.set(s, o = /* @__PURE__ */ new Map()), r === "setter" && ((i = Object.create(i)).wrapped = !0), o.set(e.name, i), r === "accessor") {
    const { name: n } = e;
    return { set(c) {
      const a = t.get.call(this);
      t.set.call(this, c), this.requestUpdate(n, a, i, !0, c);
    }, init(c) {
      return c !== void 0 && this.C(n, void 0, i, c), c;
    } };
  }
  if (r === "setter") {
    const { name: n } = e;
    return function(c) {
      const a = this[n];
      t.call(this, c), this.requestUpdate(n, a, i, !0, c);
    };
  }
  throw Error("Unsupported decorator location: " + r);
};
function h(i) {
  return (t, e) => typeof e == "object" ? ee(i, t, e) : ((r, s, o) => {
    const n = s.hasOwnProperty(o);
    return s.constructor.createProperty(o, r), n ? Object.getOwnPropertyDescriptor(s, o) : void 0;
  })(i, t, e);
}
function D(i) {
  return h({ ...i, state: !0, attribute: !1 });
}
const ie = (i, t, e) => (e.configurable = !0, e.enumerable = !0, Reflect.decorate && typeof t != "object" && Object.defineProperty(i, t, e), e);
function lt(i, t) {
  return (e, r, s) => {
    const o = (n) => n.renderRoot?.querySelector(i) ?? null;
    return ie(e, r, { get() {
      return o(this);
    } });
  };
}
const U = O`
  :host {
    --cb-action-heating: var(--cb-color-heat, var(--state-climate-heat-color, #d9603f));
    --cb-action-cooling: var(--cb-color-cool, var(--state-climate-cool-color, #2f7fcc));
    --cb-action-idle: var(--cb-color-idle, var(--state-inactive-color, #888888));
    --cb-action-unknown: var(--cb-color-unknown, var(--disabled-color, #bdbdbd));

    --cb-track-bg: var(--divider-color, #e0e0e0);
    --cb-text-primary: var(--primary-text-color, #212121);
    --cb-text-secondary: var(--secondary-text-color, #727272);

    --cb-radius-card: 12px;
    --cb-radius-pill: 999px;
    --cb-gap-xs: 4px;
    --cb-gap-sm: 8px;
    --cb-gap-md: 12px;
    --cb-gap-lg: 16px;
  }
`;
function ht(i) {
  switch (i) {
    case "heating":
      return "var(--cb-action-heating)";
    case "cooling":
      return "var(--cb-action-cooling)";
    case "idle":
      return "var(--cb-action-idle)";
    default:
      return "var(--cb-action-unknown)";
  }
}
function dt(i) {
  return i === "heating" || i === "cooling" || i === "idle" ? i : "unknown";
}
function Pt(i) {
  return i.charAt(0).toUpperCase() + i.slice(1);
}
var re = Object.defineProperty, se = Object.getOwnPropertyDescriptor, q = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? se(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && re(t, e, s), s;
};
const it = 15, Ot = 28, oe = Ot - it;
function et(i) {
  return Number.isNaN(i) || !Number.isFinite(i) ? 0 : (Math.max(it, Math.min(Ot, i)) - it) / oe * 100;
}
let P = class extends b {
  constructor() {
    super(...arguments), this.low = NaN, this.high = NaN, this.room = NaN, this.action = "unknown";
  }
  render() {
    const i = dt(this.action), t = ht(i), e = Number.isFinite(this.low), r = Number.isFinite(this.high), s = Number.isFinite(this.room), o = e ? et(this.low) : 0, n = r ? et(this.high) : 100, c = Math.min(o, n), a = Math.max(0, Math.abs(n - o)), p = s ? et(this.room) : 50, d = (m) => Number.isFinite(m) ? `${m.toFixed(1)}°` : "—", l = `Comfort band gauge: low ${d(this.low)}, room ${d(this.room)}, high ${d(this.high)}, action ${i}`;
    return f`
      <svg viewBox="0 0 100 24" preserveAspectRatio="none" role="img" aria-label=${l}>
        ${W`<rect class="track" x="0" y="10" width="100" height="4" rx="2"></rect>`}
        ${e && r ? W`<rect class="band" x=${c} y="9" width=${a} height="6" rx="3" fill=${t}></rect>` : null}
        ${s ? W`<circle cx=${p} cy="12" r="4.5" fill=${t}></circle>` : null}
        ${s ? W`<circle class="marker-ring" cx=${p} cy="12" r="3" stroke=${t}></circle>` : null}
      </svg>
    `;
  }
};
P.styles = [
  U,
  O`
      :host {
        display: block;
        width: 100%;
      }
      svg {
        display: block;
        width: 100%;
        height: 24px;
        overflow: visible;
      }
      .track {
        fill: var(--cb-track-bg);
      }
      .band {
        opacity: 0.85;
      }
      .marker-ring {
        fill: var(--ha-card-background, var(--card-background-color, #ffffff));
        stroke-width: 2;
      }
      .label {
        font-size: 11px;
        fill: var(--cb-text-secondary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
      }
    `
];
q([
  h({ type: Number })
], P.prototype, "low", 2);
q([
  h({ type: Number })
], P.prototype, "high", 2);
q([
  h({ type: Number })
], P.prototype, "room", 2);
q([
  h({ type: String })
], P.prototype, "action", 2);
P = q([
  M("band-gauge")
], P);
var ne = Object.defineProperty, ae = Object.getOwnPropertyDescriptor, $ = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? ae(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && ne(t, e, s), s;
};
let v = class extends b {
  constructor() {
    super(...arguments), this.zoneName = "", this.roomTemp = NaN, this.low = NaN, this.high = NaN, this.action = "unknown", this.overrideActive = !1, this.overrideEnds = null, this.noExpand = !1;
  }
  _onTap(i) {
    this.noExpand || i instanceof KeyboardEvent && i.key !== "Enter" && i.key !== " " || (i.preventDefault(), this.dispatchEvent(new CustomEvent("comfort-band-tile-tap", { bubbles: !0, composed: !0 })));
  }
  _renderRoomTemp() {
    return Number.isFinite(this.roomTemp) ? `${this.roomTemp.toFixed(1)}°` : "—";
  }
  _renderOverridePill() {
    if (!this.overrideActive) return null;
    const i = ce(this.overrideEnds);
    return f`<div class="override-pill">Override${i ? ` · ${i}` : ""}</div>`;
  }
  _renderActionChip() {
    const i = dt(this.action);
    if (i === "idle" || i === "unknown") return null;
    const t = ht(i);
    return f`<span class="action-chip" style="background:${t}">
      ${Pt(i)}
    </span>`;
  }
  render() {
    return f`
      <div
        class="tile ${this.noExpand ? "no-expand" : ""}"
        role="${this.noExpand ? "group" : "button"}"
        tabindex="${this.noExpand ? -1 : 0}"
        @click=${this._onTap}
        @keydown=${this._onTap}
      >
        <div class="header">
          <div class="zone-name">${this.zoneName || "—"}</div>
          ${this._renderActionChip()}
        </div>
        <div class="body">
          <div class="room-temp">${this._renderRoomTemp()}</div>
          <div class="gauge-wrap">
            <band-gauge
              .low=${this.low}
              .high=${this.high}
              .room=${this.roomTemp}
              .action=${this.action}
            ></band-gauge>
          </div>
        </div>
        ${this._renderOverridePill()}
      </div>
    `;
  }
};
v.styles = [
  U,
  O`
      :host {
        display: block;
      }
      .tile {
        display: flex;
        flex-direction: column;
        gap: var(--cb-gap-sm);
        padding: var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #ffffff));
        box-shadow: var(--ha-card-box-shadow, none);
        cursor: pointer;
        transition: transform 0.12s ease;
      }
      .tile.no-expand {
        cursor: default;
      }
      .tile:not(.no-expand):hover {
        transform: translateY(-1px);
      }
      .tile:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 2px;
      }
      .header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: var(--cb-gap-sm);
      }
      .zone-name {
        font-size: 14px;
        font-weight: 500;
        color: var(--cb-text-primary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
      }
      .action-chip {
        font-size: 11px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 2px 8px;
        border-radius: var(--cb-radius-pill);
        color: var(--cb-text-on-action, #ffffff);
      }
      .body {
        display: flex;
        align-items: center;
        gap: var(--cb-gap-md);
      }
      .room-temp {
        font-size: 32px;
        font-weight: 300;
        color: var(--cb-text-primary);
        font-variant-numeric: tabular-nums;
        line-height: 1;
        min-width: 70px;
      }
      .gauge-wrap {
        flex: 1;
        min-width: 0;
      }
      .override-pill {
        align-self: flex-start;
        font-size: 11px;
        padding: 2px 8px;
        border-radius: var(--cb-radius-pill);
        background: var(--cb-text-secondary);
        color: var(--cb-text-on-action, #ffffff);
        opacity: 0.85;
      }
    `
];
$([
  h({ type: String })
], v.prototype, "zoneName", 2);
$([
  h({ type: Number })
], v.prototype, "roomTemp", 2);
$([
  h({ type: Number })
], v.prototype, "low", 2);
$([
  h({ type: Number })
], v.prototype, "high", 2);
$([
  h({ type: String })
], v.prototype, "action", 2);
$([
  h({ type: Boolean })
], v.prototype, "overrideActive", 2);
$([
  h({ type: String })
], v.prototype, "overrideEnds", 2);
$([
  h({ type: Boolean })
], v.prototype, "noExpand", 2);
v = $([
  M("comfort-band-tile")
], v);
function ce(i) {
  if (!i) return "";
  const t = Date.parse(i);
  if (Number.isNaN(t)) return "";
  const e = t - Date.now();
  if (e <= 0) return "";
  const r = Math.round(e / 6e4);
  if (r < 60) return `${r}m left`;
  const s = Math.floor(r / 60), o = r % 60;
  return o ? `${s}h ${o}m left` : `${s}h left`;
}
var le = Object.defineProperty, he = Object.getOwnPropertyDescriptor, y = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? he(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && le(t, e, s), s;
};
let g = class extends b {
  constructor() {
    super(...arguments), this.min = 16, this.max = 26, this.step = 0.5, this.low = 19, this.high = 22, this.unit = "°", this._dragging = null, this._onThumbPointerDown = (i, t) => {
      i.preventDefault();
      const e = i.currentTarget;
      e.setPointerCapture(i.pointerId), this._dragging = t;
      const r = (o) => {
        this._setHandle(t, this._xToValue(o.clientX)) && this._fire("input");
      }, s = (o) => {
        e.releasePointerCapture(o.pointerId), e.removeEventListener("pointermove", r), e.removeEventListener("pointerup", s), e.removeEventListener("pointercancel", s), this._dragging = null, this._fire("change");
      };
      e.addEventListener("pointermove", r), e.addEventListener("pointerup", s), e.addEventListener("pointercancel", s);
    }, this._onTrackPointerDown = (i) => {
      if (i.target.classList.contains("thumb")) return;
      const t = this._xToValue(i.clientX), e = (this.low + this.high) / 2, r = t < e ? "low" : "high";
      this._setHandle(r, t) && this._fire("change");
    }, this._onKeyDown = (i, t) => {
      let e = 0;
      switch (i.key) {
        case "ArrowLeft":
        case "ArrowDown":
          e = -this.step;
          break;
        case "ArrowRight":
        case "ArrowUp":
          e = this.step;
          break;
        case "Home":
          i.preventDefault(), this._setHandle(t, this.min) && this._fire("change");
          return;
        case "End":
          i.preventDefault(), this._setHandle(t, this.max) && this._fire("change");
          return;
        default:
          return;
      }
      i.preventDefault();
      const r = t === "low" ? this.low : this.high;
      this._setHandle(t, r + e) && this._fire("change");
    };
  }
  _pct(i) {
    const t = this.max - this.min;
    return t <= 0 ? 0 : (i - this.min) / t * 100;
  }
  _snap(i) {
    const t = Math.round((i - this.min) / this.step) * this.step + this.min;
    return Math.max(this.min, Math.min(this.max, t));
  }
  _setHandle(i, t) {
    const e = this._snap(t);
    if (i === "low") {
      const r = Math.min(e, this.high - this.step);
      if (r === this.low) return !1;
      this.low = r;
    } else {
      const r = Math.max(e, this.low + this.step);
      if (r === this.high) return !1;
      this.high = r;
    }
    return !0;
  }
  _xToValue(i) {
    const t = this._track?.getBoundingClientRect();
    if (!t || t.width === 0) return this.min;
    const e = Math.max(0, Math.min(1, (i - t.left) / t.width));
    return this.min + e * (this.max - this.min);
  }
  _fire(i) {
    this.dispatchEvent(
      new CustomEvent(i, {
        detail: { low: this.low, high: this.high },
        bubbles: !0,
        composed: !0
      })
    );
  }
  _fmt(i) {
    return `${i.toFixed(1)}${this.unit}`;
  }
  render() {
    const i = this._pct(this.low), t = this._pct(this.high);
    return f`
      <div class="track" @pointerdown=${this._onTrackPointerDown}>
        <div class="fill" style="left:${i}%; width:${t - i}%"></div>
        <div
          class="thumb ${this._dragging === "low" ? "dragging" : ""}"
          style="left:${i}%"
          tabindex="0"
          role="slider"
          aria-label="Lower bound"
          aria-valuemin=${this.min}
          aria-valuemax=${this.high - this.step}
          aria-valuenow=${this.low}
          aria-valuetext=${this._fmt(this.low)}
          @pointerdown=${(e) => this._onThumbPointerDown(e, "low")}
          @keydown=${(e) => this._onKeyDown(e, "low")}
        ></div>
        <div
          class="thumb ${this._dragging === "high" ? "dragging" : ""}"
          style="left:${t}%"
          tabindex="0"
          role="slider"
          aria-label="Upper bound"
          aria-valuemin=${this.low + this.step}
          aria-valuemax=${this.max}
          aria-valuenow=${this.high}
          aria-valuetext=${this._fmt(this.high)}
          @pointerdown=${(e) => this._onThumbPointerDown(e, "high")}
          @keydown=${(e) => this._onKeyDown(e, "high")}
        ></div>
      </div>
      <div class="label-row">
        <span class="value-low">${this._fmt(this.low)}</span>
        <span class="value-high">${this._fmt(this.high)}</span>
      </div>
    `;
  }
};
g.styles = [
  U,
  O`
      :host {
        display: block;
        padding: 16px 12px;
        --thumb-size: 20px;
      }
      .track {
        position: relative;
        height: 6px;
        background: var(--cb-track-bg);
        border-radius: 3px;
        cursor: pointer;
      }
      .fill {
        position: absolute;
        top: 0;
        height: 100%;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        opacity: 0.6;
        border-radius: 3px;
        pointer-events: none;
      }
      .thumb {
        position: absolute;
        top: 50%;
        width: var(--thumb-size);
        height: var(--thumb-size);
        margin-left: calc(var(--thumb-size) / -2);
        margin-top: calc(var(--thumb-size) / -2);
        background: var(--ha-card-background, #ffffff);
        border: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        border-radius: 50%;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
        cursor: grab;
        touch-action: none;
        transition: transform 0.1s ease;
      }
      .thumb:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 3px;
      }
      .thumb.dragging {
        cursor: grabbing;
        transform: scale(1.15);
      }
      .label-row {
        display: flex;
        justify-content: space-between;
        font-size: 12px;
        color: var(--cb-text-secondary);
        margin-top: 14px;
        font-variant-numeric: tabular-nums;
      }
      .value-low,
      .value-high {
        font-size: 14px;
        font-weight: 500;
        color: var(--cb-text-primary);
      }
    `
];
y([
  h({ type: Number })
], g.prototype, "min", 2);
y([
  h({ type: Number })
], g.prototype, "max", 2);
y([
  h({ type: Number })
], g.prototype, "step", 2);
y([
  h({ type: Number })
], g.prototype, "low", 2);
y([
  h({ type: Number })
], g.prototype, "high", 2);
y([
  h({ type: String })
], g.prototype, "unit", 2);
y([
  D()
], g.prototype, "_dragging", 2);
y([
  lt(".track")
], g.prototype, "_track", 2);
g = y([
  M("dual-handle-slider")
], g);
const Tt = "comfort_band";
function de(i, t) {
  const e = { zone: t.zone };
  return t.low !== void 0 && (e.low = t.low), t.high !== void 0 && (e.high = t.high), t.hours !== void 0 && (e.hours = t.hours), i.callService(Tt, "start_override", e);
}
function pe(i, t) {
  return i.callService(Tt, "cancel_override", { ...t });
}
var ue = Object.defineProperty, fe = Object.getOwnPropertyDescriptor, R = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? fe(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && ue(t, e, s), s;
};
const me = [1, 3, 6];
let A = class extends b {
  constructor() {
    super(...arguments), this.zone = "", this._pendingLow = null, this._pendingHigh = null, this._onSliderInput = (i) => {
      this._pendingLow = i.detail.low, this._pendingHigh = i.detail.high;
    }, this._onSliderChange = (i) => {
      !this.hass || !this.zone || (this._pendingLow = null, this._pendingHigh = null, de(this.hass, {
        zone: this.zone,
        low: i.detail.low,
        high: i.detail.high
      }));
    }, this._onCancel = () => {
      !this.hass || !this.zone || pe(this.hass, { zone: this.zone });
    }, this._onPickHours = (i) => {
      !this.hass || !this.entities?.overrideHours || this.hass.callService("number", "set_value", {
        entity_id: this.entities.overrideHours,
        value: i
      });
    };
  }
  get _stateOf() {
    const i = this.hass?.states ?? {};
    return (t) => t !== null ? i[t] : void 0;
  }
  _numericState(i) {
    const t = this._stateOf(i);
    if (!t) return NaN;
    const e = parseFloat(t.state);
    return Number.isFinite(e) ? e : NaN;
  }
  render() {
    if (!this.hass || !this.entities) return u;
    const i = this._numericState(this.entities.manualLow), t = this._numericState(this.entities.manualHigh), e = this._numericState(this.entities.effectiveLow), r = this._numericState(this.entities.effectiveHigh), s = this._numericState(this.entities.roomTemperature), o = this._numericState(this.entities.overrideHours), n = this._stateOf(this.entities.currentAction)?.state ?? "unknown", c = this._stateOf(this.entities.overrideActive)?.state === "on", a = this._pendingLow ?? (Number.isFinite(i) ? i : 19), p = this._pendingHigh ?? (Number.isFinite(t) ? t : 22), d = dt(n), l = d !== "idle" && d !== "unknown";
    return f`
      <div class="header-row">
        <div class="room-temp">${Number.isFinite(s) ? `${s.toFixed(1)}°` : "—"}</div>
        ${l ? f`<span class="action-chip" style="background:${ht(d)}"
              >${Pt(d)}</span
            >` : u}
      </div>
      <div class="gauge-row">
        <band-gauge .low=${e} .high=${r} .room=${s} .action=${n}></band-gauge>
      </div>

      <section>
        <h3>Manual band</h3>
        <dual-handle-slider
          .min=${16}
          .max=${26}
          .step=${0.5}
          .low=${a}
          .high=${p}
          @input=${this._onSliderInput}
          @change=${this._onSliderChange}
        ></dual-handle-slider>
      </section>

      ${this._renderOverrideSection(c)} ${this._renderHoursSection(o)}
    `;
  }
  _renderOverrideSection(i) {
    if (!i) return u;
    const t = this._stateOf(this.entities.overrideEnds)?.state, e = ve(t ?? null);
    return f`
      <section>
        <h3>Override</h3>
        <div class="override-row">
          <span>Active${e ? ` · ${e}` : ""}</span>
          <button class="button secondary" @click=${this._onCancel}>Cancel</button>
        </div>
      </section>
    `;
  }
  _renderHoursSection(i) {
    return this.entities?.overrideHours ? f`
      <section>
        <h3>Override duration</h3>
        <div class="preset-row">
          ${me.map(
      (t) => f`
              <button
                class="preset ${i === t ? "active" : ""}"
                @click=${() => this._onPickHours(t)}
              >
                ${t} h
              </button>
            `
    )}
        </div>
      </section>
    ` : u;
  }
};
A.styles = [
  U,
  O`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .gauge-row {
        margin-bottom: var(--cb-gap-md);
      }
      .header-row {
        display: flex;
        align-items: baseline;
        gap: var(--cb-gap-sm);
        margin-bottom: var(--cb-gap-sm);
      }
      .room-temp {
        font-size: 36px;
        font-weight: 300;
        color: var(--cb-text-primary);
        font-variant-numeric: tabular-nums;
        line-height: 1;
      }
      .action-chip {
        font-size: 11px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 2px 8px;
        border-radius: var(--cb-radius-pill);
        color: var(--cb-text-on-action, #ffffff);
      }
      section {
        margin-top: var(--cb-gap-lg);
      }
      h3 {
        margin: 0 0 var(--cb-gap-sm);
        font-size: 13px;
        font-weight: 500;
        color: var(--cb-text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .override-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--cb-gap-sm);
        padding: var(--cb-gap-sm) var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--cb-track-bg);
        font-size: 13px;
        color: var(--cb-text-primary);
      }
      .button {
        font: inherit;
        padding: 6px 12px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid transparent;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
        cursor: pointer;
      }
      .button.secondary {
        background: transparent;
        border-color: var(--divider-color, #cccccc);
        color: var(--cb-text-primary);
      }
      .preset-row {
        display: flex;
        gap: var(--cb-gap-sm);
      }
      .preset {
        font: inherit;
        padding: 4px 10px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid var(--divider-color, #cccccc);
        background: transparent;
        color: var(--cb-text-primary);
        cursor: pointer;
      }
      .preset.active {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
        border-color: transparent;
      }
    `
];
R([
  h({ attribute: !1 })
], A.prototype, "hass", 2);
R([
  h({ type: String })
], A.prototype, "zone", 2);
R([
  h({ attribute: !1 })
], A.prototype, "entities", 2);
R([
  D()
], A.prototype, "_pendingLow", 2);
R([
  D()
], A.prototype, "_pendingHigh", 2);
A = R([
  M("comfort-band-now-tab")
], A);
function ve(i) {
  if (!i) return "";
  const t = Date.parse(i);
  if (Number.isNaN(t)) return "";
  const e = t - Date.now();
  if (e <= 0) return "";
  const r = Math.round(e / 6e4);
  if (r < 60) return `${r}m left`;
  const s = Math.floor(r / 60), o = r % 60;
  return o ? `${s}h ${o}m left` : `${s}h left`;
}
var ge = Object.defineProperty, be = Object.getOwnPropertyDescriptor, E = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? be(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && ge(t, e, s), s;
};
const _e = [
  { id: "now", label: "Now" },
  { id: "schedule", label: "Schedule" },
  { id: "profiles", label: "Profiles" },
  { id: "insights", label: "Insights" }
];
let _ = class extends b {
  constructor() {
    super(...arguments), this.zone = "", this.zoneName = "", this._activeTab = "now", this._isOpen = !1, this._onClose = () => {
      this._isOpen = !1, this.dispatchEvent(
        new CustomEvent("comfort-band-modal-close", { bubbles: !0, composed: !0 })
      );
    }, this._onSelectTab = (i) => {
      this._activeTab = i;
    };
  }
  open() {
    this._isOpen = !0, this.updateComplete.then(() => this._dialog?.showModal());
  }
  close() {
    this._dialog?.close();
  }
  selectTab(i) {
    this._activeTab = i;
  }
  render() {
    if (!this._isOpen) return u;
    const i = this.zoneName || this.zone || "Comfort Band";
    return f`
      <dialog @close=${this._onClose}>
        <div class="frame">
          <header>
            <h2>${i}</h2>
            <button class="close" @click=${this.close} aria-label="Close">×</button>
          </header>
          <nav role="tablist">
            ${_e.map(
      (t) => f`
                <button
                  role="tab"
                  aria-selected=${this._activeTab === t.id}
                  @click=${() => this._onSelectTab(t.id)}
                >
                  ${t.label}
                </button>
              `
    )}
          </nav>
          <div class="panel" role="tabpanel">${this._renderTab()}</div>
        </div>
      </dialog>
    `;
  }
  _renderTab() {
    switch (this._activeTab) {
      case "now":
        return f`<comfort-band-now-tab
          .hass=${this.hass}
          .zone=${this.zone}
          .entities=${this.entities}
        ></comfort-band-now-tab>`;
      case "schedule":
        return f`<div class="placeholder">Schedule editor — landing in commit 7.</div>`;
      case "profiles":
        return f`<div class="placeholder">Profiles — landing in commit 5.</div>`;
      case "insights":
        return f`<div class="placeholder">Insights — landing in commit 6.</div>`;
    }
  }
};
_.styles = [
  U,
  O`
      :host {
        --cb-modal-max-width: 480px;
      }
      dialog {
        width: min(90vw, var(--cb-modal-max-width));
        max-height: min(90vh, 720px);
        padding: 0;
        border: none;
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #ffffff));
        color: var(--cb-text-primary);
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
        overflow: hidden;
      }
      dialog::backdrop {
        background: rgba(0, 0, 0, 0.4);
      }
      .frame {
        display: flex;
        flex-direction: column;
        max-height: inherit;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: var(--cb-gap-md);
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
      }
      header h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 500;
        color: var(--cb-text-primary);
      }
      .close {
        font: inherit;
        font-size: 22px;
        line-height: 1;
        background: transparent;
        border: none;
        color: var(--cb-text-secondary);
        cursor: pointer;
        padding: 4px 8px;
      }
      nav {
        display: flex;
        gap: 0;
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
        overflow-x: auto;
      }
      nav button {
        font: inherit;
        font-size: 13px;
        padding: 10px 14px;
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        color: var(--cb-text-secondary);
        cursor: pointer;
        white-space: nowrap;
      }
      nav button[aria-selected='true'] {
        color: var(--cb-accent, var(--primary-color, #03a9f4));
        border-bottom-color: var(--cb-accent, var(--primary-color, #03a9f4));
      }
      .panel {
        overflow-y: auto;
        flex: 1;
      }
      .placeholder {
        padding: var(--cb-gap-lg);
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
      }
    `
];
E([
  h({ attribute: !1 })
], _.prototype, "hass", 2);
E([
  h({ type: String })
], _.prototype, "zone", 2);
E([
  h({ type: String })
], _.prototype, "zoneName", 2);
E([
  h({ attribute: !1 })
], _.prototype, "entities", 2);
E([
  D()
], _.prototype, "_activeTab", 2);
E([
  D()
], _.prototype, "_isOpen", 2);
E([
  lt("dialog")
], _.prototype, "_dialog", 2);
_ = E([
  M("comfort-band-modal")
], _);
const zt = "comfort_band", $e = {
  effective_low: "effectiveLow",
  effective_high: "effectiveHigh",
  room_temperature: "roomTemperature",
  override_ends: "overrideEnds",
  current_action: "currentAction",
  override_active: "overrideActive",
  manual_low: "manualLow",
  manual_high: "manualHigh",
  override_hours: "overrideHours",
  deadband_below: "deadbandBelow",
  deadband_above: "deadbandAbove",
  min_cycle_minutes: "minCycleMinutes",
  cancel_override: "cancelOverride",
  enabled: "enabled"
};
function ye() {
  return {
    effectiveLow: null,
    effectiveHigh: null,
    roomTemperature: null,
    overrideEnds: null,
    currentAction: null,
    overrideActive: null,
    manualLow: null,
    manualHigh: null,
    overrideHours: null,
    deadbandBelow: null,
    deadbandAbove: null,
    minCycleMinutes: null,
    cancelOverride: null,
    enabled: null,
    deviceId: null,
    deviceName: null
  };
}
function we(i, t) {
  for (const e of Object.values(i.devices))
    for (const [r, s] of e.identifiers)
      if (r === t[0] && s === t[1])
        return e;
  return null;
}
function xe(i, t) {
  return Object.values(i.entities).filter(
    (e) => e.device_id === t && e.platform === zt
  );
}
function Ae(i, t) {
  const e = ye(), r = we(i, [zt, `zone:${t}`]);
  if (r === null) return e;
  e.deviceId = r.id, e.deviceName = r.name_by_user ?? r.name;
  const s = `${t}_`;
  for (const o of xe(i, r.id)) {
    if (!o.unique_id.startsWith(s)) continue;
    const n = o.unique_id.slice(s.length), c = $e[n];
    c !== void 0 && (e[c] = o.entity_id);
  }
  return e;
}
var Ee = Object.defineProperty, Se = Object.getOwnPropertyDescriptor, Q = (i, t, e, r) => {
  for (var s = r > 1 ? void 0 : r ? Se(t, e) : t, o = i.length - 1, n; o >= 0; o--)
    (n = i[o]) && (s = (r ? n(t, e, s) : n(s)) || s);
  return r && s && Ee(t, e, s), s;
};
let k = class extends b {
  constructor() {
    super(...arguments), this._onTileTap = () => {
      this._modal?.open();
    };
  }
  setConfig(i) {
    if (!i?.zone)
      throw new Error("comfort-band-card: `zone` is required");
    this._config = i;
  }
  /** HA's panel/grid uses this to size the card. ~1 row per ~50 px of content. */
  getCardSize() {
    return 2;
  }
  render() {
    if (!this._config || !this.hass) return f``;
    const i = this._config.zone, t = Ae(this.hass, i);
    if (t.deviceId === null)
      return f`<div class="placeholder">
        Comfort Band zone <code>${i}</code> not found. Add it via Settings → Devices &
        Services.
      </div>`;
    const e = this._config.compact === !0, r = this._buildView(this.hass, t);
    return f`
      <comfort-band-tile
        zoneName=${r.zoneName}
        .roomTemp=${r.roomTemp}
        .low=${r.low}
        .high=${r.high}
        .action=${r.action}
        .overrideActive=${r.overrideActive}
        .overrideEnds=${r.overrideEnds}
        .noExpand=${e}
        @comfort-band-tile-tap=${this._onTileTap}
      ></comfort-band-tile>
      ${e ? null : f`<comfort-band-modal
            .hass=${this.hass}
            zone=${i}
            zoneName=${r.zoneName}
            .entities=${t}
          ></comfort-band-modal>`}
    `;
  }
  _buildView(i, t) {
    const e = (s) => s !== null ? i.states[s] : void 0, r = (s) => {
      const o = e(s);
      if (!o) return NaN;
      const n = parseFloat(o.state);
      return Number.isFinite(n) ? n : NaN;
    };
    return {
      zoneName: t.deviceName ?? this._config.zone,
      low: r(t.effectiveLow),
      high: r(t.effectiveHigh),
      roomTemp: r(t.roomTemperature),
      action: e(t.currentAction)?.state ?? "unknown",
      overrideActive: e(t.overrideActive)?.state === "on",
      overrideEnds: e(t.overrideEnds)?.state ?? null
    };
  }
};
k.styles = [
  U,
  O`
      :host {
        display: block;
      }
      .placeholder {
        padding: var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #fff));
        color: var(--cb-text-secondary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
        font-size: 13px;
      }
    `
];
Q([
  h({ attribute: !1 })
], k.prototype, "hass", 2);
Q([
  D()
], k.prototype, "_config", 2);
Q([
  lt("comfort-band-modal")
], k.prototype, "_modal", 2);
k = Q([
  M("comfort-band-card")
], k);
(window.customCards ??= []).push({
  type: "comfort-band-card",
  name: "Comfort Band",
  description: "Schedule editor and live status for a Comfort Band zone.",
  preview: !1
});
console.info(
  "%c COMFORT-BAND-CARD %c v0.1.0-dev ",
  "color:white;background:#2196F3;padding:2px 4px;border-radius:3px",
  "color:#000;background:#fff;padding:2px 4px;border-radius:3px"
);
