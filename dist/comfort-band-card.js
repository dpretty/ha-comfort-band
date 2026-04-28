const tt = globalThis, mt = tt.ShadowRoot && (tt.ShadyCSS === void 0 || tt.ShadyCSS.nativeShadow) && "adoptedStyleSheets" in Document.prototype && "replace" in CSSStyleSheet.prototype, bt = Symbol(), Et = /* @__PURE__ */ new WeakMap();
let jt = class {
  constructor(t, i, r) {
    if (this._$cssResult$ = !0, r !== bt) throw Error("CSSResult is not constructable. Use `unsafeCSS` or `css` instead.");
    this.cssText = t, this.t = i;
  }
  get styleSheet() {
    let t = this.o;
    const i = this.t;
    if (mt && t === void 0) {
      const r = i !== void 0 && i.length === 1;
      r && (t = Et.get(i)), t === void 0 && ((this.o = t = new CSSStyleSheet()).replaceSync(this.cssText), r && Et.set(i, t));
    }
    return t;
  }
  toString() {
    return this.cssText;
  }
};
const Yt = (e) => new jt(typeof e == "string" ? e : e + "", void 0, bt), v = (e, ...t) => {
  const i = e.length === 1 ? e[0] : t.reduce((r, s, o) => r + ((n) => {
    if (n._$cssResult$ === !0) return n.cssText;
    if (typeof n == "number") return n;
    throw Error("Value passed to 'css' function must be a 'css' function result: " + n + ". Use 'unsafeCSS' to pass non-literal values, but take care to ensure page security.");
  })(s) + e[o + 1], e[0]);
  return new jt(i, e, bt);
}, Qt = (e, t) => {
  if (mt) e.adoptedStyleSheets = t.map((i) => i instanceof CSSStyleSheet ? i : i.styleSheet);
  else for (const i of t) {
    const r = document.createElement("style"), s = tt.litNonce;
    s !== void 0 && r.setAttribute("nonce", s), r.textContent = i.cssText, e.appendChild(r);
  }
}, St = mt ? (e) => e : (e) => e instanceof CSSStyleSheet ? ((t) => {
  let i = "";
  for (const r of t.cssRules) i += r.cssText;
  return Yt(i);
})(e) : e;
const { is: te, defineProperty: ee, getOwnPropertyDescriptor: ie, getOwnPropertyNames: re, getOwnPropertySymbols: se, getPrototypeOf: oe } = Object, ot = globalThis, Ct = ot.trustedTypes, ne = Ct ? Ct.emptyScript : "", ae = ot.reactiveElementPolyfillSupport, V = (e, t) => e, et = { toAttribute(e, t) {
  switch (t) {
    case Boolean:
      e = e ? ne : null;
      break;
    case Object:
    case Array:
      e = e == null ? e : JSON.stringify(e);
  }
  return e;
}, fromAttribute(e, t) {
  let i = e;
  switch (t) {
    case Boolean:
      i = e !== null;
      break;
    case Number:
      i = e === null ? null : Number(e);
      break;
    case Object:
    case Array:
      try {
        i = JSON.parse(e);
      } catch {
        i = null;
      }
  }
  return i;
} }, vt = (e, t) => !te(e, t), Pt = { attribute: !0, type: String, converter: et, reflect: !1, useDefault: !1, hasChanged: vt };
Symbol.metadata ??= Symbol("metadata"), ot.litPropertyMetadata ??= /* @__PURE__ */ new WeakMap();
let U = class extends HTMLElement {
  static addInitializer(t) {
    this._$Ei(), (this.l ??= []).push(t);
  }
  static get observedAttributes() {
    return this.finalize(), this._$Eh && [...this._$Eh.keys()];
  }
  static createProperty(t, i = Pt) {
    if (i.state && (i.attribute = !1), this._$Ei(), this.prototype.hasOwnProperty(t) && ((i = Object.create(i)).wrapped = !0), this.elementProperties.set(t, i), !i.noAccessor) {
      const r = Symbol(), s = this.getPropertyDescriptor(t, r, i);
      s !== void 0 && ee(this.prototype, t, s);
    }
  }
  static getPropertyDescriptor(t, i, r) {
    const { get: s, set: o } = ie(this.prototype, t) ?? { get() {
      return this[i];
    }, set(n) {
      this[i] = n;
    } };
    return { get: s, set(n) {
      const h = s?.call(this);
      o?.call(this, n), this.requestUpdate(t, h, r);
    }, configurable: !0, enumerable: !0 };
  }
  static getPropertyOptions(t) {
    return this.elementProperties.get(t) ?? Pt;
  }
  static _$Ei() {
    if (this.hasOwnProperty(V("elementProperties"))) return;
    const t = oe(this);
    t.finalize(), t.l !== void 0 && (this.l = [...t.l]), this.elementProperties = new Map(t.elementProperties);
  }
  static finalize() {
    if (this.hasOwnProperty(V("finalized"))) return;
    if (this.finalized = !0, this._$Ei(), this.hasOwnProperty(V("properties"))) {
      const i = this.properties, r = [...re(i), ...se(i)];
      for (const s of r) this.createProperty(s, i[s]);
    }
    const t = this[Symbol.metadata];
    if (t !== null) {
      const i = litPropertyMetadata.get(t);
      if (i !== void 0) for (const [r, s] of i) this.elementProperties.set(r, s);
    }
    this._$Eh = /* @__PURE__ */ new Map();
    for (const [i, r] of this.elementProperties) {
      const s = this._$Eu(i, r);
      s !== void 0 && this._$Eh.set(s, i);
    }
    this.elementStyles = this.finalizeStyles(this.styles);
  }
  static finalizeStyles(t) {
    const i = [];
    if (Array.isArray(t)) {
      const r = new Set(t.flat(1 / 0).reverse());
      for (const s of r) i.unshift(St(s));
    } else t !== void 0 && i.push(St(t));
    return i;
  }
  static _$Eu(t, i) {
    const r = i.attribute;
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
    const t = /* @__PURE__ */ new Map(), i = this.constructor.elementProperties;
    for (const r of i.keys()) this.hasOwnProperty(r) && (t.set(r, this[r]), delete this[r]);
    t.size > 0 && (this._$Ep = t);
  }
  createRenderRoot() {
    const t = this.shadowRoot ?? this.attachShadow(this.constructor.shadowRootOptions);
    return Qt(t, this.constructor.elementStyles), t;
  }
  connectedCallback() {
    this.renderRoot ??= this.createRenderRoot(), this.enableUpdating(!0), this._$EO?.forEach((t) => t.hostConnected?.());
  }
  enableUpdating(t) {
  }
  disconnectedCallback() {
    this._$EO?.forEach((t) => t.hostDisconnected?.());
  }
  attributeChangedCallback(t, i, r) {
    this._$AK(t, r);
  }
  _$ET(t, i) {
    const r = this.constructor.elementProperties.get(t), s = this.constructor._$Eu(t, r);
    if (s !== void 0 && r.reflect === !0) {
      const o = (r.converter?.toAttribute !== void 0 ? r.converter : et).toAttribute(i, r.type);
      this._$Em = t, o == null ? this.removeAttribute(s) : this.setAttribute(s, o), this._$Em = null;
    }
  }
  _$AK(t, i) {
    const r = this.constructor, s = r._$Eh.get(t);
    if (s !== void 0 && this._$Em !== s) {
      const o = r.getPropertyOptions(s), n = typeof o.converter == "function" ? { fromAttribute: o.converter } : o.converter?.fromAttribute !== void 0 ? o.converter : et;
      this._$Em = s;
      const h = n.fromAttribute(i, o.type);
      this[s] = h ?? this._$Ej?.get(s) ?? h, this._$Em = null;
    }
  }
  requestUpdate(t, i, r, s = !1, o) {
    if (t !== void 0) {
      const n = this.constructor;
      if (s === !1 && (o = this[t]), r ??= n.getPropertyOptions(t), !((r.hasChanged ?? vt)(o, i) || r.useDefault && r.reflect && o === this._$Ej?.get(t) && !this.hasAttribute(n._$Eu(t, r)))) return;
      this.C(t, i, r);
    }
    this.isUpdatePending === !1 && (this._$ES = this._$EP());
  }
  C(t, i, { useDefault: r, reflect: s, wrapped: o }, n) {
    r && !(this._$Ej ??= /* @__PURE__ */ new Map()).has(t) && (this._$Ej.set(t, n ?? i ?? this[t]), o !== !0 || n !== void 0) || (this._$AL.has(t) || (this.hasUpdated || r || (i = void 0), this._$AL.set(t, i)), s === !0 && this._$Em !== t && (this._$Eq ??= /* @__PURE__ */ new Set()).add(t));
  }
  async _$EP() {
    this.isUpdatePending = !0;
    try {
      await this._$ES;
    } catch (i) {
      Promise.reject(i);
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
        const { wrapped: n } = o, h = this[s];
        n !== !0 || this._$AL.has(s) || h === void 0 || this.C(s, void 0, o, h);
      }
    }
    let t = !1;
    const i = this._$AL;
    try {
      t = this.shouldUpdate(i), t ? (this.willUpdate(i), this._$EO?.forEach((r) => r.hostUpdate?.()), this.update(i)) : this._$EM();
    } catch (r) {
      throw t = !1, this._$EM(), r;
    }
    t && this._$AE(i);
  }
  willUpdate(t) {
  }
  _$AE(t) {
    this._$EO?.forEach((i) => i.hostUpdated?.()), this.hasUpdated || (this.hasUpdated = !0, this.firstUpdated(t)), this.updated(t);
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
    this._$Eq &&= this._$Eq.forEach((i) => this._$ET(i, this[i])), this._$EM();
  }
  updated(t) {
  }
  firstUpdated(t) {
  }
};
U.elementStyles = [], U.shadowRootOptions = { mode: "open" }, U[V("elementProperties")] = /* @__PURE__ */ new Map(), U[V("finalized")] = /* @__PURE__ */ new Map(), ae?.({ ReactiveElement: U }), (ot.reactiveElementVersions ??= []).push("2.1.2");
const gt = globalThis, Ot = (e) => e, it = gt.trustedTypes, Tt = it ? it.createPolicy("lit-html", { createHTML: (e) => e }) : void 0, Rt = "$lit$", T = `lit$${Math.random().toFixed(9).slice(2)}$`, Bt = "?" + T, ce = `<${Bt}>`, H = document, W = () => H.createComment(""), K = (e) => e === null || typeof e != "object" && typeof e != "function", _t = Array.isArray, le = (e) => _t(e) || typeof e?.[Symbol.iterator] == "function", dt = `[ 	
\f\r]`, q = /<(?:(!--|\/[^a-zA-Z])|(\/?[a-zA-Z][^>\s]*)|(\/?$))/g, Nt = /-->/g, zt = />/g, D = RegExp(`>|${dt}(?:([^\\s"'>=/]+)(${dt}*=${dt}*(?:[^ 	
\f\r"'\`<>=]|("|')|))|$)`, "g"), kt = /'/g, Dt = /"/g, It = /^(?:script|style|textarea|title)$/i, Ft = (e) => (t, ...i) => ({ _$litType$: e, strings: t, values: i }), a = Ft(1), Q = Ft(2), j = Symbol.for("lit-noChange"), p = Symbol.for("lit-nothing"), Mt = /* @__PURE__ */ new WeakMap(), M = H.createTreeWalker(H, 129);
function qt(e, t) {
  if (!_t(e) || !e.hasOwnProperty("raw")) throw Error("invalid template strings array");
  return Tt !== void 0 ? Tt.createHTML(t) : t;
}
const he = (e, t) => {
  const i = e.length - 1, r = [];
  let s, o = t === 2 ? "<svg>" : t === 3 ? "<math>" : "", n = q;
  for (let h = 0; h < i; h++) {
    const c = e[h];
    let f, u, d = -1, g = 0;
    for (; g < c.length && (n.lastIndex = g, u = n.exec(c), u !== null); ) g = n.lastIndex, n === q ? u[1] === "!--" ? n = Nt : u[1] !== void 0 ? n = zt : u[2] !== void 0 ? (It.test(u[2]) && (s = RegExp("</" + u[2], "g")), n = D) : u[3] !== void 0 && (n = D) : n === D ? u[0] === ">" ? (n = s ?? q, d = -1) : u[1] === void 0 ? d = -2 : (d = n.lastIndex - u[2].length, f = u[1], n = u[3] === void 0 ? D : u[3] === '"' ? Dt : kt) : n === Dt || n === kt ? n = D : n === Nt || n === zt ? n = q : (n = D, s = void 0);
    const O = n === D && e[h + 1].startsWith("/>") ? " " : "";
    o += n === q ? c + ce : d >= 0 ? (r.push(f), c.slice(0, d) + Rt + c.slice(d) + T + O) : c + T + (d === -2 ? h : O);
  }
  return [qt(e, o + (e[i] || "<?>") + (t === 2 ? "</svg>" : t === 3 ? "</math>" : "")), r];
};
class Z {
  constructor({ strings: t, _$litType$: i }, r) {
    let s;
    this.parts = [];
    let o = 0, n = 0;
    const h = t.length - 1, c = this.parts, [f, u] = he(t, i);
    if (this.el = Z.createElement(f, r), M.currentNode = this.el.content, i === 2 || i === 3) {
      const d = this.el.content.firstChild;
      d.replaceWith(...d.childNodes);
    }
    for (; (s = M.nextNode()) !== null && c.length < h; ) {
      if (s.nodeType === 1) {
        if (s.hasAttributes()) for (const d of s.getAttributeNames()) if (d.endsWith(Rt)) {
          const g = u[n++], O = s.getAttribute(d).split(T), Y = /([.?@])?(.*)/.exec(g);
          c.push({ type: 1, index: o, name: Y[2], strings: O, ctor: Y[1] === "." ? pe : Y[1] === "?" ? ue : Y[1] === "@" ? fe : nt }), s.removeAttribute(d);
        } else d.startsWith(T) && (c.push({ type: 6, index: o }), s.removeAttribute(d));
        if (It.test(s.tagName)) {
          const d = s.textContent.split(T), g = d.length - 1;
          if (g > 0) {
            s.textContent = it ? it.emptyScript : "";
            for (let O = 0; O < g; O++) s.append(d[O], W()), M.nextNode(), c.push({ type: 2, index: ++o });
            s.append(d[g], W());
          }
        }
      } else if (s.nodeType === 8) if (s.data === Bt) c.push({ type: 2, index: o });
      else {
        let d = -1;
        for (; (d = s.data.indexOf(T, d + 1)) !== -1; ) c.push({ type: 7, index: o }), d += T.length - 1;
      }
      o++;
    }
  }
  static createElement(t, i) {
    const r = H.createElement("template");
    return r.innerHTML = t, r;
  }
}
function R(e, t, i = e, r) {
  if (t === j) return t;
  let s = r !== void 0 ? i._$Co?.[r] : i._$Cl;
  const o = K(t) ? void 0 : t._$litDirective$;
  return s?.constructor !== o && (s?._$AO?.(!1), o === void 0 ? s = void 0 : (s = new o(e), s._$AT(e, i, r)), r !== void 0 ? (i._$Co ??= [])[r] = s : i._$Cl = s), s !== void 0 && (t = R(e, s._$AS(e, t.values), s, r)), t;
}
class de {
  constructor(t, i) {
    this._$AV = [], this._$AN = void 0, this._$AD = t, this._$AM = i;
  }
  get parentNode() {
    return this._$AM.parentNode;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  u(t) {
    const { el: { content: i }, parts: r } = this._$AD, s = (t?.creationScope ?? H).importNode(i, !0);
    M.currentNode = s;
    let o = M.nextNode(), n = 0, h = 0, c = r[0];
    for (; c !== void 0; ) {
      if (n === c.index) {
        let f;
        c.type === 2 ? f = new G(o, o.nextSibling, this, t) : c.type === 1 ? f = new c.ctor(o, c.name, c.strings, this, t) : c.type === 6 && (f = new me(o, this, t)), this._$AV.push(f), c = r[++h];
      }
      n !== c?.index && (o = M.nextNode(), n++);
    }
    return M.currentNode = H, s;
  }
  p(t) {
    let i = 0;
    for (const r of this._$AV) r !== void 0 && (r.strings !== void 0 ? (r._$AI(t, r, i), i += r.strings.length - 2) : r._$AI(t[i])), i++;
  }
}
class G {
  get _$AU() {
    return this._$AM?._$AU ?? this._$Cv;
  }
  constructor(t, i, r, s) {
    this.type = 2, this._$AH = p, this._$AN = void 0, this._$AA = t, this._$AB = i, this._$AM = r, this.options = s, this._$Cv = s?.isConnected ?? !0;
  }
  get parentNode() {
    let t = this._$AA.parentNode;
    const i = this._$AM;
    return i !== void 0 && t?.nodeType === 11 && (t = i.parentNode), t;
  }
  get startNode() {
    return this._$AA;
  }
  get endNode() {
    return this._$AB;
  }
  _$AI(t, i = this) {
    t = R(this, t, i), K(t) ? t === p || t == null || t === "" ? (this._$AH !== p && this._$AR(), this._$AH = p) : t !== this._$AH && t !== j && this._(t) : t._$litType$ !== void 0 ? this.$(t) : t.nodeType !== void 0 ? this.T(t) : le(t) ? this.k(t) : this._(t);
  }
  O(t) {
    return this._$AA.parentNode.insertBefore(t, this._$AB);
  }
  T(t) {
    this._$AH !== t && (this._$AR(), this._$AH = this.O(t));
  }
  _(t) {
    this._$AH !== p && K(this._$AH) ? this._$AA.nextSibling.data = t : this.T(H.createTextNode(t)), this._$AH = t;
  }
  $(t) {
    const { values: i, _$litType$: r } = t, s = typeof r == "number" ? this._$AC(t) : (r.el === void 0 && (r.el = Z.createElement(qt(r.h, r.h[0]), this.options)), r);
    if (this._$AH?._$AD === s) this._$AH.p(i);
    else {
      const o = new de(s, this), n = o.u(this.options);
      o.p(i), this.T(n), this._$AH = o;
    }
  }
  _$AC(t) {
    let i = Mt.get(t.strings);
    return i === void 0 && Mt.set(t.strings, i = new Z(t)), i;
  }
  k(t) {
    _t(this._$AH) || (this._$AH = [], this._$AR());
    const i = this._$AH;
    let r, s = 0;
    for (const o of t) s === i.length ? i.push(r = new G(this.O(W()), this.O(W()), this, this.options)) : r = i[s], r._$AI(o), s++;
    s < i.length && (this._$AR(r && r._$AB.nextSibling, s), i.length = s);
  }
  _$AR(t = this._$AA.nextSibling, i) {
    for (this._$AP?.(!1, !0, i); t !== this._$AB; ) {
      const r = Ot(t).nextSibling;
      Ot(t).remove(), t = r;
    }
  }
  setConnected(t) {
    this._$AM === void 0 && (this._$Cv = t, this._$AP?.(t));
  }
}
class nt {
  get tagName() {
    return this.element.tagName;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  constructor(t, i, r, s, o) {
    this.type = 1, this._$AH = p, this._$AN = void 0, this.element = t, this.name = i, this._$AM = s, this.options = o, r.length > 2 || r[0] !== "" || r[1] !== "" ? (this._$AH = Array(r.length - 1).fill(new String()), this.strings = r) : this._$AH = p;
  }
  _$AI(t, i = this, r, s) {
    const o = this.strings;
    let n = !1;
    if (o === void 0) t = R(this, t, i, 0), n = !K(t) || t !== this._$AH && t !== j, n && (this._$AH = t);
    else {
      const h = t;
      let c, f;
      for (t = o[0], c = 0; c < o.length - 1; c++) f = R(this, h[r + c], i, c), f === j && (f = this._$AH[c]), n ||= !K(f) || f !== this._$AH[c], f === p ? t = p : t !== p && (t += (f ?? "") + o[c + 1]), this._$AH[c] = f;
    }
    n && !s && this.j(t);
  }
  j(t) {
    t === p ? this.element.removeAttribute(this.name) : this.element.setAttribute(this.name, t ?? "");
  }
}
class pe extends nt {
  constructor() {
    super(...arguments), this.type = 3;
  }
  j(t) {
    this.element[this.name] = t === p ? void 0 : t;
  }
}
class ue extends nt {
  constructor() {
    super(...arguments), this.type = 4;
  }
  j(t) {
    this.element.toggleAttribute(this.name, !!t && t !== p);
  }
}
class fe extends nt {
  constructor(t, i, r, s, o) {
    super(t, i, r, s, o), this.type = 5;
  }
  _$AI(t, i = this) {
    if ((t = R(this, t, i, 0) ?? p) === j) return;
    const r = this._$AH, s = t === p && r !== p || t.capture !== r.capture || t.once !== r.once || t.passive !== r.passive, o = t !== p && (r === p || s);
    s && this.element.removeEventListener(this.name, this, r), o && this.element.addEventListener(this.name, this, t), this._$AH = t;
  }
  handleEvent(t) {
    typeof this._$AH == "function" ? this._$AH.call(this.options?.host ?? this.element, t) : this._$AH.handleEvent(t);
  }
}
class me {
  constructor(t, i, r) {
    this.element = t, this.type = 6, this._$AN = void 0, this._$AM = i, this.options = r;
  }
  get _$AU() {
    return this._$AM._$AU;
  }
  _$AI(t) {
    R(this, t);
  }
}
const be = gt.litHtmlPolyfillSupport;
be?.(Z, G), (gt.litHtmlVersions ??= []).push("3.3.2");
const ve = (e, t, i) => {
  const r = i?.renderBefore ?? t;
  let s = r._$litPart$;
  if (s === void 0) {
    const o = i?.renderBefore ?? null;
    r._$litPart$ = s = new G(t.insertBefore(W(), o), o, void 0, i ?? {});
  }
  return s._$AI(e), s;
};
const $t = globalThis;
class b extends U {
  constructor() {
    super(...arguments), this.renderOptions = { host: this }, this._$Do = void 0;
  }
  createRenderRoot() {
    const t = super.createRenderRoot();
    return this.renderOptions.renderBefore ??= t.firstChild, t;
  }
  update(t) {
    const i = this.render();
    this.hasUpdated || (this.renderOptions.isConnected = this.isConnected), super.update(t), this._$Do = ve(i, this.renderRoot, this.renderOptions);
  }
  connectedCallback() {
    super.connectedCallback(), this._$Do?.setConnected(!0);
  }
  disconnectedCallback() {
    super.disconnectedCallback(), this._$Do?.setConnected(!1);
  }
  render() {
    return j;
  }
}
b._$litElement$ = !0, b.finalized = !0, $t.litElementHydrateSupport?.({ LitElement: b });
const ge = $t.litElementPolyfillSupport;
ge?.({ LitElement: b });
($t.litElementVersions ??= []).push("4.2.2");
const $ = (e) => (t, i) => {
  i !== void 0 ? i.addInitializer(() => {
    customElements.define(e, t);
  }) : customElements.define(e, t);
};
const _e = { attribute: !0, type: String, converter: et, reflect: !1, hasChanged: vt }, $e = (e = _e, t, i) => {
  const { kind: r, metadata: s } = i;
  let o = globalThis.litPropertyMetadata.get(s);
  if (o === void 0 && globalThis.litPropertyMetadata.set(s, o = /* @__PURE__ */ new Map()), r === "setter" && ((e = Object.create(e)).wrapped = !0), o.set(i.name, e), r === "accessor") {
    const { name: n } = i;
    return { set(h) {
      const c = t.get.call(this);
      t.set.call(this, h), this.requestUpdate(n, c, e, !0, h);
    }, init(h) {
      return h !== void 0 && this.C(n, void 0, e, h), h;
    } };
  }
  if (r === "setter") {
    const { name: n } = i;
    return function(h) {
      const c = this[n];
      t.call(this, h), this.requestUpdate(n, c, e, !0, h);
    };
  }
  throw Error("Unsupported decorator location: " + r);
};
function l(e) {
  return (t, i) => typeof i == "object" ? $e(e, t, i) : ((r, s, o) => {
    const n = s.hasOwnProperty(o);
    return s.constructor.createProperty(o, r), n ? Object.getOwnPropertyDescriptor(s, o) : void 0;
  })(e, t, i);
}
function m(e) {
  return l({ ...e, state: !0, attribute: !1 });
}
const ye = (e, t, i) => (i.configurable = !0, i.enumerable = !0, Reflect.decorate && typeof t != "object" && Object.defineProperty(e, t, i), i);
function at(e, t) {
  return (i, r, s) => {
    const o = (n) => n.renderRoot?.querySelector(e) ?? null;
    return ye(i, r, { get() {
      return o(this);
    } });
  };
}
const y = v`
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
function yt(e) {
  switch (e) {
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
function wt(e) {
  return e === "heating" || e === "cooling" || e === "idle" ? e : "unknown";
}
function Vt(e) {
  return e.charAt(0).toUpperCase() + e.slice(1);
}
var we = Object.defineProperty, xe = Object.getOwnPropertyDescriptor, J = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? xe(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && we(t, i, s), s;
};
const ft = 15, Wt = 28, Ae = Wt - ft;
function pt(e) {
  return Number.isNaN(e) || !Number.isFinite(e) ? 0 : (Math.max(ft, Math.min(Wt, e)) - ft) / Ae * 100;
}
let L = class extends b {
  constructor() {
    super(...arguments), this.low = NaN, this.high = NaN, this.room = NaN, this.action = "unknown";
  }
  render() {
    const e = wt(this.action), t = yt(e), i = Number.isFinite(this.low), r = Number.isFinite(this.high), s = Number.isFinite(this.room), o = i ? pt(this.low) : 0, n = r ? pt(this.high) : 100, h = Math.min(o, n), c = Math.max(0, Math.abs(n - o)), f = s ? pt(this.room) : 50, u = (g) => Number.isFinite(g) ? `${g.toFixed(1)}°` : "—", d = `Comfort band gauge: low ${u(this.low)}, room ${u(this.room)}, high ${u(this.high)}, action ${e}`;
    return a`
      <svg viewBox="0 0 100 24" preserveAspectRatio="none" role="img" aria-label=${d}>
        ${Q`<rect class="track" x="0" y="10" width="100" height="4" rx="2"></rect>`}
        ${i && r ? Q`<rect class="band" x=${h} y="9" width=${c} height="6" rx="3" fill=${t}></rect>` : null}
        ${s ? Q`<circle cx=${f} cy="12" r="4.5" fill=${t}></circle>` : null}
        ${s ? Q`<circle class="marker-ring" cx=${f} cy="12" r="3" stroke=${t}></circle>` : null}
      </svg>
    `;
  }
};
L.styles = [
  y,
  v`
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
J([
  l({ type: Number })
], L.prototype, "low", 2);
J([
  l({ type: Number })
], L.prototype, "high", 2);
J([
  l({ type: Number })
], L.prototype, "room", 2);
J([
  l({ type: String })
], L.prototype, "action", 2);
L = J([
  $("band-gauge")
], L);
var Ee = Object.defineProperty, Se = Object.getOwnPropertyDescriptor, C = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? Se(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && Ee(t, i, s), s;
};
let w = class extends b {
  constructor() {
    super(...arguments), this.zoneName = "", this.roomTemp = NaN, this.low = NaN, this.high = NaN, this.action = "unknown", this.overrideActive = !1, this.overrideEnds = null, this.noExpand = !1;
  }
  _onTap(e) {
    this.noExpand || e instanceof KeyboardEvent && e.key !== "Enter" && e.key !== " " || (e.preventDefault(), this.dispatchEvent(new CustomEvent("comfort-band-tile-tap", { bubbles: !0, composed: !0 })));
  }
  _renderRoomTemp() {
    return Number.isFinite(this.roomTemp) ? `${this.roomTemp.toFixed(1)}°` : "—";
  }
  _renderOverridePill() {
    if (!this.overrideActive) return null;
    const e = Ce(this.overrideEnds);
    return a`<div class="override-pill">Override${e ? ` · ${e}` : ""}</div>`;
  }
  _renderActionChip() {
    const e = wt(this.action);
    if (e === "idle" || e === "unknown") return null;
    const t = yt(e);
    return a`<span class="action-chip" style="background:${t}">
      ${Vt(e)}
    </span>`;
  }
  render() {
    return a`
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
w.styles = [
  y,
  v`
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
C([
  l({ type: String })
], w.prototype, "zoneName", 2);
C([
  l({ type: Number })
], w.prototype, "roomTemp", 2);
C([
  l({ type: Number })
], w.prototype, "low", 2);
C([
  l({ type: Number })
], w.prototype, "high", 2);
C([
  l({ type: String })
], w.prototype, "action", 2);
C([
  l({ type: Boolean })
], w.prototype, "overrideActive", 2);
C([
  l({ type: String })
], w.prototype, "overrideEnds", 2);
C([
  l({ type: Boolean })
], w.prototype, "noExpand", 2);
w = C([
  $("comfort-band-tile")
], w);
function Ce(e) {
  if (!e) return "";
  const t = Date.parse(e);
  if (Number.isNaN(t)) return "";
  const i = t - Date.now();
  if (i <= 0) return "";
  const r = Math.round(i / 6e4);
  if (r < 60) return `${r}m left`;
  const s = Math.floor(r / 60), o = r % 60;
  return o ? `${s}h ${o}m left` : `${s}h left`;
}
var Pe = Object.defineProperty, Oe = Object.getOwnPropertyDescriptor, P = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? Oe(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && Pe(t, i, s), s;
};
let x = class extends b {
  constructor() {
    super(...arguments), this.min = 16, this.max = 26, this.step = 0.5, this.low = 19, this.high = 22, this.unit = "°", this._dragging = null, this._onThumbPointerDown = (e, t) => {
      e.preventDefault();
      const i = e.currentTarget;
      i.setPointerCapture(e.pointerId), this._dragging = t;
      const r = (o) => {
        this._setHandle(t, this._xToValue(o.clientX)) && this._fire("input");
      }, s = (o) => {
        i.releasePointerCapture(o.pointerId), i.removeEventListener("pointermove", r), i.removeEventListener("pointerup", s), i.removeEventListener("pointercancel", s), this._dragging = null, this._fire("change");
      };
      i.addEventListener("pointermove", r), i.addEventListener("pointerup", s), i.addEventListener("pointercancel", s);
    }, this._onTrackPointerDown = (e) => {
      if (e.target.classList.contains("thumb")) return;
      const t = this._xToValue(e.clientX), i = (this.low + this.high) / 2, r = t < i ? "low" : "high";
      this._setHandle(r, t) && this._fire("change");
    }, this._onKeyDown = (e, t) => {
      let i = 0;
      switch (e.key) {
        case "ArrowLeft":
        case "ArrowDown":
          i = -this.step;
          break;
        case "ArrowRight":
        case "ArrowUp":
          i = this.step;
          break;
        case "Home":
          e.preventDefault(), this._setHandle(t, this.min) && this._fire("change");
          return;
        case "End":
          e.preventDefault(), this._setHandle(t, this.max) && this._fire("change");
          return;
        default:
          return;
      }
      e.preventDefault();
      const r = t === "low" ? this.low : this.high;
      this._setHandle(t, r + i) && this._fire("change");
    };
  }
  _pct(e) {
    const t = this.max - this.min;
    return t <= 0 ? 0 : (e - this.min) / t * 100;
  }
  _snap(e) {
    const t = Math.round((e - this.min) / this.step) * this.step + this.min;
    return Math.max(this.min, Math.min(this.max, t));
  }
  _setHandle(e, t) {
    const i = this._snap(t);
    if (e === "low") {
      const r = Math.min(i, this.high - this.step);
      if (r === this.low) return !1;
      this.low = r;
    } else {
      const r = Math.max(i, this.low + this.step);
      if (r === this.high) return !1;
      this.high = r;
    }
    return !0;
  }
  _xToValue(e) {
    const t = this._track?.getBoundingClientRect();
    if (!t || t.width === 0) return this.min;
    const i = Math.max(0, Math.min(1, (e - t.left) / t.width));
    return this.min + i * (this.max - this.min);
  }
  _fire(e) {
    this.dispatchEvent(
      new CustomEvent(e, {
        detail: { low: this.low, high: this.high },
        bubbles: !0,
        composed: !0
      })
    );
  }
  _fmt(e) {
    return `${e.toFixed(1)}${this.unit}`;
  }
  render() {
    const e = this._pct(this.low), t = this._pct(this.high);
    return a`
      <div class="track" @pointerdown=${this._onTrackPointerDown}>
        <div class="fill" style="left:${e}%; width:${t - e}%"></div>
        <div
          class="thumb ${this._dragging === "low" ? "dragging" : ""}"
          style="left:${e}%"
          tabindex="0"
          role="slider"
          aria-label="Lower bound"
          aria-valuemin=${this.min}
          aria-valuemax=${this.high - this.step}
          aria-valuenow=${this.low}
          aria-valuetext=${this._fmt(this.low)}
          @pointerdown=${(i) => this._onThumbPointerDown(i, "low")}
          @keydown=${(i) => this._onKeyDown(i, "low")}
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
          @pointerdown=${(i) => this._onThumbPointerDown(i, "high")}
          @keydown=${(i) => this._onKeyDown(i, "high")}
        ></div>
      </div>
      <div class="label-row">
        <span class="value-low">${this._fmt(this.low)}</span>
        <span class="value-high">${this._fmt(this.high)}</span>
      </div>
    `;
  }
};
x.styles = [
  y,
  v`
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
P([
  l({ type: Number })
], x.prototype, "min", 2);
P([
  l({ type: Number })
], x.prototype, "max", 2);
P([
  l({ type: Number })
], x.prototype, "step", 2);
P([
  l({ type: Number })
], x.prototype, "low", 2);
P([
  l({ type: Number })
], x.prototype, "high", 2);
P([
  l({ type: String })
], x.prototype, "unit", 2);
P([
  m()
], x.prototype, "_dragging", 2);
P([
  at(".track")
], x.prototype, "_track", 2);
x = P([
  $("dual-handle-slider")
], x);
const ct = "comfort_band";
function Te(e, t) {
  return e.callWS({
    type: "comfort_band/get_schedule",
    ...t
  });
}
function Ne(e, t) {
  return e.callService(ct, "set_schedule", { ...t });
}
function ze(e, t) {
  const i = { zone: t.zone };
  return t.low !== void 0 && (i.low = t.low), t.high !== void 0 && (i.high = t.high), t.hours !== void 0 && (i.hours = t.hours), e.callService(ct, "start_override", i);
}
function ke(e, t) {
  return e.callService(ct, "cancel_override", { ...t });
}
function De(e, t) {
  return e.callService(ct, "set_profile", { ...t });
}
var Me = Object.defineProperty, He = Object.getOwnPropertyDescriptor, F = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? He(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && Me(t, i, s), s;
};
const Le = [1, 3, 6];
let N = class extends b {
  constructor() {
    super(...arguments), this.zone = "", this._pendingLow = null, this._pendingHigh = null, this._onSliderInput = (e) => {
      this._pendingLow = e.detail.low, this._pendingHigh = e.detail.high;
    }, this._onSliderChange = (e) => {
      !this.hass || !this.zone || (this._pendingLow = null, this._pendingHigh = null, ze(this.hass, {
        zone: this.zone,
        low: e.detail.low,
        high: e.detail.high
      }));
    }, this._onCancel = () => {
      !this.hass || !this.zone || ke(this.hass, { zone: this.zone });
    }, this._onPickHours = (e) => {
      !this.hass || !this.entities?.overrideHours || this.hass.callService("number", "set_value", {
        entity_id: this.entities.overrideHours,
        value: e
      });
    };
  }
  get _stateOf() {
    const e = this.hass?.states ?? {};
    return (t) => t !== null ? e[t] : void 0;
  }
  _numericState(e) {
    const t = this._stateOf(e);
    if (!t) return NaN;
    const i = parseFloat(t.state);
    return Number.isFinite(i) ? i : NaN;
  }
  render() {
    if (!this.hass || !this.entities) return p;
    const e = this._numericState(this.entities.manualLow), t = this._numericState(this.entities.manualHigh), i = this._numericState(this.entities.effectiveLow), r = this._numericState(this.entities.effectiveHigh), s = this._numericState(this.entities.roomTemperature), o = this._numericState(this.entities.overrideHours), n = this._stateOf(this.entities.currentAction)?.state ?? "unknown", h = this._stateOf(this.entities.overrideActive)?.state === "on", c = this._pendingLow ?? (Number.isFinite(e) ? e : 19), f = this._pendingHigh ?? (Number.isFinite(t) ? t : 22), u = wt(n), d = u !== "idle" && u !== "unknown";
    return a`
      <div class="header-row">
        <div class="room-temp">${Number.isFinite(s) ? `${s.toFixed(1)}°` : "—"}</div>
        ${d ? a`<span class="action-chip" style="background:${yt(u)}"
              >${Vt(u)}</span
            >` : p}
      </div>
      <div class="gauge-row">
        <band-gauge .low=${i} .high=${r} .room=${s} .action=${n}></band-gauge>
      </div>

      <section>
        <h3>Manual band</h3>
        <dual-handle-slider
          .min=${16}
          .max=${26}
          .step=${0.5}
          .low=${c}
          .high=${f}
          @input=${this._onSliderInput}
          @change=${this._onSliderChange}
        ></dual-handle-slider>
      </section>

      ${this._renderOverrideSection(h)} ${this._renderHoursSection(o)}
    `;
  }
  _renderOverrideSection(e) {
    if (!e) return p;
    const t = this._stateOf(this.entities.overrideEnds)?.state, i = Ue(t ?? null);
    return a`
      <section>
        <h3>Override</h3>
        <div class="override-row">
          <span>Active${i ? ` · ${i}` : ""}</span>
          <button class="button secondary" @click=${this._onCancel}>Cancel</button>
        </div>
      </section>
    `;
  }
  _renderHoursSection(e) {
    return this.entities?.overrideHours ? a`
      <section>
        <h3>Override duration</h3>
        <div class="preset-row">
          ${Le.map(
      (t) => a`
              <button
                class="preset ${e === t ? "active" : ""}"
                @click=${() => this._onPickHours(t)}
              >
                ${t} h
              </button>
            `
    )}
        </div>
      </section>
    ` : p;
  }
};
N.styles = [
  y,
  v`
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
F([
  l({ attribute: !1 })
], N.prototype, "hass", 2);
F([
  l({ type: String })
], N.prototype, "zone", 2);
F([
  l({ attribute: !1 })
], N.prototype, "entities", 2);
F([
  m()
], N.prototype, "_pendingLow", 2);
F([
  m()
], N.prototype, "_pendingHigh", 2);
N = F([
  $("comfort-band-now-tab")
], N);
function Ue(e) {
  if (!e) return "";
  const t = Date.parse(e);
  if (Number.isNaN(t)) return "";
  const i = t - Date.now();
  if (i <= 0) return "";
  const r = Math.round(i / 6e4);
  if (r < 60) return `${r}m left`;
  const s = Math.floor(r / 60), o = r % 60;
  return o ? `${s}h ${o}m left` : `${s}h left`;
}
const xt = "comfort_band", je = {
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
function Re() {
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
function Kt(e, t) {
  for (const i of Object.values(e.devices))
    for (const [r, s] of i.identifiers)
      if (r === t[0] && s === t[1])
        return i;
  return null;
}
function Zt(e, t) {
  return Object.values(e.entities).filter(
    (i) => i.device_id === t && i.platform === xt
  );
}
function Be(e, t) {
  const i = Re(), r = Kt(e, [xt, `zone:${t}`]);
  if (r === null) return i;
  i.deviceId = r.id, i.deviceName = r.name_by_user ?? r.name;
  const s = `${t}_`;
  for (const o of Zt(e, r.id)) {
    if (!o.unique_id.startsWith(s)) continue;
    const n = o.unique_id.slice(s.length), h = je[n];
    h !== void 0 && (i[h] = o.entity_id);
  }
  return i;
}
function Xt(e) {
  const t = Kt(e, [xt, "profile_manager"]);
  if (t === null) return null;
  for (const i of Zt(e, t.id))
    if (i.unique_id === "profile_manager_active_profile")
      return i.entity_id;
  return null;
}
var Ie = Object.defineProperty, Fe = Object.getOwnPropertyDescriptor, Gt = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? Fe(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && Ie(t, i, s), s;
};
let rt = class extends b {
  _onSelect(e) {
    this.hass && De(this.hass, { profile: e });
  }
  render() {
    if (!this.hass) return p;
    const e = Xt(this.hass);
    if (e === null)
      return a`<div class="empty">Profile manager not registered yet.</div>`;
    const t = this.hass.states[e], i = t?.attributes.options, r = Array.isArray(i) ? i.filter((o) => typeof o == "string") : [], s = t?.state ?? "";
    return r.length === 0 ? a`<div class="empty">No profiles configured.</div>` : a`
      <ul role="listbox" aria-label="Profiles">
        ${r.map(
      (o) => a`
            <li
              role="option"
              tabindex="0"
              class=${o === s ? "active" : ""}
              aria-selected=${o === s}
              @click=${() => this._onSelect(o)}
              @keydown=${(n) => {
        (n.key === "Enter" || n.key === " ") && (n.preventDefault(), this._onSelect(o));
      }}
            >
              <span class="name">${o}</span>
              ${o === s ? a`<span class="badge">Active</span>` : p}
            </li>
          `
    )}
      </ul>
      <div class="footer">Create / rename / delete profiles in a future release.</div>
    `;
  }
};
rt.styles = [
  y,
  v`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .empty {
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
        padding: var(--cb-gap-lg);
      }
      ul {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: var(--cb-gap-sm);
      }
      li {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: var(--cb-gap-sm) var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--cb-track-bg);
        cursor: pointer;
        font-size: 14px;
        color: var(--cb-text-primary);
      }
      li.active {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
      }
      li:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 2px;
      }
      .name {
        font-weight: 500;
        text-transform: capitalize;
      }
      .badge {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        opacity: 0.85;
      }
      .footer {
        margin-top: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
        text-align: center;
      }
    `
];
Gt([
  l({ attribute: !1 })
], rt.prototype, "hass", 2);
rt = Gt([
  $("comfort-band-profiles-tab")
], rt);
var qe = Object.defineProperty, Ve = Object.getOwnPropertyDescriptor, lt = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? Ve(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && qe(t, i, s), s;
};
let B = class extends b {
  constructor() {
    super(...arguments), this._graphAvailable = null, this._graphCard = null;
  }
  async firstUpdated() {
    await this._maybeMountGraph();
  }
  updated(e) {
    (e.has("entities") || e.has("hass")) && this._maybeMountGraph();
  }
  async _maybeMountGraph() {
    const e = this.entities?.roomTemperature;
    if (!(!e || !this.hass)) {
      if (this._graphCard) {
        this._graphCard.hass = this.hass;
        return;
      }
      if (typeof window.loadCardHelpers != "function") {
        this._graphAvailable = !1;
        return;
      }
      try {
        const i = (await window.loadCardHelpers()).createCardElement({
          type: "history-graph",
          entities: [e],
          hours_to_show: 24
        });
        i.hass = this.hass;
        const r = this.renderRoot.querySelector(".graph-container");
        r && (r.innerHTML = "", r.appendChild(i), this._graphCard = i, this._graphAvailable = !0);
      } catch {
        this._graphAvailable = !1;
      }
    }
  }
  render() {
    const e = this.entities?.roomTemperature;
    return e ? a`
      <div class="graph-container"></div>
      ${this._graphAvailable === !1 ? a`<div class="fallback">
            Inline graph unavailable.
            <a href="/history?entity_id=${e}" target="_blank" rel="noopener"
              >Open in HA history →</a
            >
          </div>` : p}
    ` : a`<div class="empty">No room temperature sensor for this zone.</div>`;
  }
};
B.styles = [
  y,
  v`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .graph-container {
        min-height: 220px;
      }
      .graph-container :first-child {
        --ha-card-box-shadow: none;
      }
      .fallback,
      .empty {
        padding: var(--cb-gap-lg);
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
      }
      .fallback a {
        color: var(--cb-accent, var(--primary-color, #03a9f4));
        text-decoration: none;
        margin-left: 8px;
      }
    `
];
lt([
  l({ attribute: !1 })
], B.prototype, "hass", 2);
lt([
  l({ attribute: !1 })
], B.prototype, "entities", 2);
lt([
  m()
], B.prototype, "_graphAvailable", 2);
B = lt([
  $("comfort-band-insights-tab")
], B);
var We = Object.defineProperty, Ke = Object.getOwnPropertyDescriptor, Jt = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? Ke(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && We(t, i, s), s;
};
const Ze = 500, Xe = 1.5, Ge = 15, Ht = [0, 6, 12, 18, 24];
function Lt(e) {
  const t = /^(\d{1,2}):(\d{2})$/.exec(e);
  return t ? parseInt(t[1], 10) * 60 + parseInt(t[2], 10) : 0;
}
function Je(e) {
  const t = Math.max(0, Math.min(1439, e)), i = Math.floor(t / 60), r = t % 60;
  return `${i.toString().padStart(2, "0")}:${r.toString().padStart(2, "0")}`;
}
function ut(e) {
  return e / (24 * 60) * 100;
}
function Ye(e, t) {
  return Math.round(e / t) * t;
}
let st = class extends b {
  constructor() {
    super(...arguments), this.transitions = [], this._longPressTimer = null, this._onTrackTap = (e) => {
      if (e.target.classList.contains("point")) return;
      const t = this.shadowRoot?.querySelector(".track");
      if (!t) return;
      const i = t.getBoundingClientRect(), r = this._xToMinutes(e.clientX, i);
      for (const s of this.transitions) {
        const o = Lt(s.at);
        if (Math.abs(ut(o) - ut(r)) < Xe) return;
      }
      this.dispatchEvent(
        new CustomEvent("transition-add", {
          detail: { at: Je(r) },
          bubbles: !0,
          composed: !0
        })
      );
    }, this._onPointTap = (e) => {
      this.dispatchEvent(
        new CustomEvent("transition-edit", {
          detail: { transition: e },
          bubbles: !0,
          composed: !0
        })
      );
    }, this._onPointPointerDown = (e, t) => {
      e.stopPropagation(), this._longPressTimer !== null && window.clearTimeout(this._longPressTimer);
      const i = e.currentTarget;
      let r = !1;
      this._longPressTimer = window.setTimeout(() => {
        r = !0, this._longPressTimer = null, this.dispatchEvent(
          new CustomEvent("transition-delete", {
            detail: { at: t.at },
            bubbles: !0,
            composed: !0
          })
        );
      }, Ze);
      const s = () => {
        i.removeEventListener("pointerup", s), i.removeEventListener("pointercancel", s), i.removeEventListener("pointerleave", s), this._longPressTimer !== null && (window.clearTimeout(this._longPressTimer), this._longPressTimer = null, r || this._onPointTap(t));
      };
      i.addEventListener("pointerup", s), i.addEventListener("pointercancel", s), i.addEventListener("pointerleave", s);
    }, this._onPointKeyDown = (e, t) => {
      e.key === "Enter" || e.key === " " ? (e.preventDefault(), this._onPointTap(t)) : (e.key === "Delete" || e.key === "Backspace") && (e.preventDefault(), this.dispatchEvent(
        new CustomEvent("transition-delete", {
          detail: { at: t.at },
          bubbles: !0,
          composed: !0
        })
      ));
    };
  }
  _xToMinutes(e, t) {
    if (t.width === 0) return 0;
    const i = Math.max(0, Math.min(1, (e - t.left) / t.width));
    return Ye(i * 24 * 60, Ge);
  }
  render() {
    return a`
      <div class="ruler">
        <div class="track" @click=${this._onTrackTap}></div>
        ${Ht.map((e, t) => {
      const i = t === 0 ? "hour-tick start" : t === Ht.length - 1 ? "hour-tick terminal" : "hour-tick";
      return a`<div class=${i} style="left:${e / 24 * 100}%">${e}h</div>`;
    })}
        ${this.transitions.map((e) => {
      const t = Lt(e.at), i = ut(t), r = `Transition at ${e.at}, low ${e.low.toFixed(1)} degrees, high ${e.high.toFixed(1)} degrees. Tap to edit, long-press or Delete to remove.`;
      return a`
            <button
              class="point"
              role="button"
              style="left:${i}%"
              tabindex="0"
              aria-label=${r}
              data-at=${e.at}
              @pointerdown=${(s) => this._onPointPointerDown(s, e)}
              @keydown=${(s) => this._onPointKeyDown(s, e)}
            ></button>
            <div class="point-label" style="left:${i}%">
              ${e.at} · ${e.low.toFixed(1)}–${e.high.toFixed(1)}°
            </div>
          `;
    })}
      </div>
      ${this.transitions.length === 0 ? a`<div class="empty-hint">Tap the timeline to add a transition.</div>` : null}
    `;
  }
};
st.styles = [
  y,
  v`
      :host {
        display: block;
      }
      .ruler {
        position: relative;
        height: 80px;
        margin: var(--cb-gap-md) 0;
      }
      .track {
        position: absolute;
        top: 36px;
        left: 0;
        right: 0;
        height: 6px;
        background: var(--cb-track-bg);
        border-radius: 3px;
        cursor: pointer;
      }
      .hour-tick {
        position: absolute;
        top: 18px;
        font-size: 10px;
        color: var(--cb-text-secondary);
        transform: translateX(-50%);
        font-variant-numeric: tabular-nums;
      }
      .hour-tick.terminal {
        transform: translateX(-100%);
      }
      .hour-tick.start {
        transform: translateX(0);
      }
      .point {
        position: absolute;
        top: 30px;
        width: 18px;
        height: 18px;
        margin-left: -9px;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        border: 2px solid var(--ha-card-background, #ffffff);
        border-radius: 50%;
        cursor: pointer;
        touch-action: none;
        z-index: 1;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
      }
      .point:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 3px;
      }
      .point-label {
        position: absolute;
        top: 56px;
        font-size: 10px;
        color: var(--cb-text-secondary);
        transform: translateX(-50%);
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
      }
      .empty-hint {
        font-size: 12px;
        color: var(--cb-text-secondary);
        text-align: center;
        margin-top: var(--cb-gap-sm);
      }
    `
];
Jt([
  l({ type: Array })
], st.prototype, "transitions", 2);
st = Jt([
  $("timeline-editor")
], st);
var Qe = Object.defineProperty, ti = Object.getOwnPropertyDescriptor, z = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? ti(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && Qe(t, i, s), s;
};
let A = class extends b {
  constructor() {
    super(...arguments), this.transition = null, this.isNew = !1, this._at = "06:00", this._low = 19, this._high = 22, this._error = null, this._onSave = () => {
      this._validate() && this.dispatchEvent(
        new CustomEvent("dialog-save", {
          detail: { transition: { at: this._at, low: this._low, high: this._high } },
          bubbles: !0,
          composed: !0
        })
      );
    }, this._onCancel = () => {
      this.dispatchEvent(new CustomEvent("dialog-cancel", { bubbles: !0, composed: !0 }));
    }, this._onDelete = () => {
      this.transition && this.dispatchEvent(
        new CustomEvent("dialog-delete", {
          detail: { at: this.transition.at },
          bubbles: !0,
          composed: !0
        })
      );
    };
  }
  willUpdate(e) {
    e.has("transition") && this.transition && (this._at = this.transition.at, this._low = this.transition.low, this._high = this.transition.high, this._error = null);
  }
  firstUpdated() {
    queueMicrotask(() => this._atInput?.focus());
  }
  _validate() {
    return /^([01]\d|2[0-3]):[0-5]\d$/.test(this._at) ? Number.isNaN(this._low) || Number.isNaN(this._high) ? (this._error = "Low and high must be numbers.", !1) : this._low >= this._high ? (this._error = "Low must be less than high.", !1) : this._low < 10 || this._high > 35 ? (this._error = "Temperatures must be between 10 °C and 35 °C.", !1) : (this._error = null, !0) : (this._error = "Time must be HH:MM (24h, e.g. 06:00).", !1);
  }
  render() {
    return a`
      <h3>${this.isNew ? "Add transition" : "Edit transition"}</h3>
      <label>
        Time (HH:MM)
        <input
          name="at"
          type="text"
          inputmode="numeric"
          .value=${this._at}
          @input=${(e) => this._at = e.target.value}
        />
      </label>
      <label>
        Low (°C)
        <input
          name="low"
          type="number"
          step="0.5"
          min="10"
          max="35"
          .value=${String(this._low)}
          @input=${(e) => this._low = parseFloat(e.target.value)}
        />
      </label>
      <label>
        High (°C)
        <input
          name="high"
          type="number"
          step="0.5"
          min="10"
          max="35"
          .value=${String(this._high)}
          @input=${(e) => this._high = parseFloat(e.target.value)}
        />
      </label>
      ${this._error ? a`<div class="error">${this._error}</div>` : null}
      <div class="actions">
        ${this.isNew ? null : a`<button class="button danger" @click=${this._onDelete}>Delete</button>`}
        <div class="spacer"></div>
        <button class="button secondary" @click=${this._onCancel}>Cancel</button>
        <button class="button primary" @click=${this._onSave}>Save</button>
      </div>
    `;
  }
};
A.styles = [
  y,
  v`
      :host {
        display: block;
        padding: var(--cb-gap-md);
        background: var(--ha-card-background, #ffffff);
        border-radius: var(--cb-radius-card);
      }
      h3 {
        margin: 0 0 var(--cb-gap-md);
        font-size: 14px;
        font-weight: 500;
      }
      label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-bottom: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
      }
      input {
        font: inherit;
        font-size: 14px;
        padding: 8px;
        border: 1px solid var(--divider-color, #cccccc);
        border-radius: 6px;
        color: var(--cb-text-primary);
        background: transparent;
      }
      input:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 1px;
      }
      .error {
        color: var(--error-color, #b71c1c);
        font-size: 12px;
        margin-bottom: var(--cb-gap-sm);
      }
      .actions {
        display: flex;
        justify-content: flex-end;
        gap: var(--cb-gap-sm);
      }
      .actions .spacer {
        flex: 1;
      }
      .button {
        font: inherit;
        padding: 6px 12px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid transparent;
        cursor: pointer;
        font-size: 13px;
      }
      .button.primary {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
      }
      .button.secondary {
        background: transparent;
        border-color: var(--divider-color, #cccccc);
        color: var(--cb-text-primary);
      }
      .button.danger {
        background: transparent;
        color: var(--error-color, #b71c1c);
        border-color: var(--divider-color, #cccccc);
      }
    `
];
z([
  l({ type: Object })
], A.prototype, "transition", 2);
z([
  l({ type: Boolean })
], A.prototype, "isNew", 2);
z([
  m()
], A.prototype, "_at", 2);
z([
  m()
], A.prototype, "_low", 2);
z([
  m()
], A.prototype, "_high", 2);
z([
  m()
], A.prototype, "_error", 2);
z([
  at('input[name="at"]')
], A.prototype, "_atInput", 2);
A = z([
  $("transition-edit-dialog")
], A);
var ei = Object.defineProperty, ii = Object.getOwnPropertyDescriptor, S = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? ii(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && ei(t, i, s), s;
};
let _ = class extends b {
  constructor() {
    super(...arguments), this.zone = "", this._profile = "", this._transitions = [], this._loading = !0, this._error = null, this._mode = "list", this._editing = null, this._newAt = "06:00", this._onAdd = (e) => {
      this._newAt = e.detail.at, this._editing = null, this._mode = "add";
    }, this._onEdit = (e) => {
      this._editing = e.detail.transition, this._mode = "edit";
    }, this._onDelete = async (e) => {
      if (!this.hass) return;
      const t = this._transitions.filter((i) => i.at !== e.detail.at);
      await this._writeSchedule(t);
    }, this._onDialogSave = async (e) => {
      const t = e.detail.transition, i = [];
      if (this._mode === "edit" && this._editing) {
        const r = this._editing.at;
        for (const s of this._transitions)
          s.at !== r && s.at !== t.at && i.push(s);
        i.push(t);
      } else {
        for (const r of this._transitions)
          r.at !== t.at && i.push(r);
        i.push(t);
      }
      i.sort((r, s) => r.at.localeCompare(s.at)), await this._writeSchedule(i), this._mode = "list", this._editing = null;
    }, this._onDialogDelete = async (e) => {
      const t = this._transitions.filter((i) => i.at !== e.detail.at);
      await this._writeSchedule(t), this._mode = "list", this._editing = null;
    }, this._onDialogCancel = () => {
      this._mode = "list", this._editing = null;
    };
  }
  willUpdate(e) {
    e.has("hass") && this.hass && this._profile === "" && (this._profile = Ut(this.hass) ?? "home", this._refresh());
  }
  updated(e) {
    if (e.has("hass") && this.hass) {
      const t = Ut(this.hass);
      t && t !== this._profile && (this._profile = t, this._refresh());
    }
  }
  async _refresh() {
    if (!(!this.hass || !this.zone || !this._profile)) {
      this._loading = !0, this._error = null;
      try {
        const e = await Te(this.hass, {
          zone: this.zone,
          profile: this._profile
        });
        this._transitions = e?.baseline ? [...e.baseline] : [];
      } catch (e) {
        this._error = e instanceof Error ? e.message : "Failed to load schedule.";
      } finally {
        this._loading = !1;
      }
    }
  }
  async _writeSchedule(e) {
    if (this.hass)
      try {
        await Ne(this.hass, {
          zone: this.zone,
          profile: this._profile,
          transitions: e
        }), this._transitions = e, this._refresh();
      } catch (t) {
        this._error = t instanceof Error ? t.message : "Failed to save schedule.";
      }
  }
  render() {
    if (!this.hass) return p;
    if (this._mode === "add" || this._mode === "edit") {
      const e = this._mode === "edit" ? this._editing : {
        at: this._newAt,
        low: ri(this._transitions),
        high: si(this._transitions)
      };
      return a`
        <transition-edit-dialog
          .transition=${e}
          .isNew=${this._mode === "add"}
          @dialog-save=${this._onDialogSave}
          @dialog-cancel=${this._onDialogCancel}
          @dialog-delete=${this._onDialogDelete}
        ></transition-edit-dialog>
      `;
    }
    return a`
      <div class="header">
        <span class="profile-label">Active profile</span>
        <span class="profile-value">${this._profile || "—"}</span>
      </div>
      ${this._loading ? a`<div class="loading">Loading schedule…</div>` : this._error ? a`<div class="error">${this._error}</div>` : a`
              <timeline-editor
                .transitions=${this._transitions}
                @transition-add=${this._onAdd}
                @transition-edit=${this._onEdit}
                @transition-delete=${this._onDelete}
              ></timeline-editor>
              ${this._renderList()}
            `}
    `;
  }
  _renderList() {
    return this._transitions.length === 0 ? p : a`
      <ul class="list">
        ${this._transitions.map(
      (e) => a`
            <li
              @click=${() => this._onEdit(new CustomEvent("transition-edit", { detail: { transition: e } }))}
            >
              <span class="at">${e.at}</span>
              <span>${e.low.toFixed(1)}° – ${e.high.toFixed(1)}°</span>
            </li>
          `
    )}
      </ul>
    `;
  }
};
_.styles = [
  y,
  v`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        margin-bottom: var(--cb-gap-sm);
      }
      .profile-label {
        font-size: 12px;
        color: var(--cb-text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .profile-value {
        font-size: 14px;
        font-weight: 500;
        text-transform: capitalize;
        color: var(--cb-text-primary);
      }
      .loading,
      .error {
        text-align: center;
        padding: var(--cb-gap-lg);
        color: var(--cb-text-secondary);
        font-size: 13px;
      }
      .error {
        color: var(--error-color, #b71c1c);
      }
      .list {
        list-style: none;
        padding: 0;
        margin: var(--cb-gap-md) 0 0;
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .list li {
        display: flex;
        justify-content: space-between;
        font-size: 13px;
        padding: 6px 8px;
        border-radius: 6px;
      }
      .list li:hover {
        background: var(--cb-track-bg);
        cursor: pointer;
      }
      .list .at {
        font-variant-numeric: tabular-nums;
        font-weight: 500;
      }
    `
];
S([
  l({ attribute: !1 })
], _.prototype, "hass", 2);
S([
  l({ type: String })
], _.prototype, "zone", 2);
S([
  m()
], _.prototype, "_profile", 2);
S([
  m()
], _.prototype, "_transitions", 2);
S([
  m()
], _.prototype, "_loading", 2);
S([
  m()
], _.prototype, "_error", 2);
S([
  m()
], _.prototype, "_mode", 2);
S([
  m()
], _.prototype, "_editing", 2);
S([
  m()
], _.prototype, "_newAt", 2);
_ = S([
  $("comfort-band-schedule-tab")
], _);
function Ut(e) {
  const t = Xt(e);
  return t ? e.states[t]?.state ?? null : null;
}
function ri(e) {
  return e.length === 0 ? 19 : e[e.length - 1].low;
}
function si(e) {
  return e.length === 0 ? 22 : e[e.length - 1].high;
}
var oi = Object.defineProperty, ni = Object.getOwnPropertyDescriptor, k = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? ni(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && oi(t, i, s), s;
};
const ai = [
  { id: "now", label: "Now" },
  { id: "schedule", label: "Schedule" },
  { id: "profiles", label: "Profiles" },
  { id: "insights", label: "Insights" }
];
let E = class extends b {
  constructor() {
    super(...arguments), this.zone = "", this.zoneName = "", this._activeTab = "now", this._isOpen = !1, this._onClose = () => {
      this._isOpen = !1, this.dispatchEvent(
        new CustomEvent("comfort-band-modal-close", { bubbles: !0, composed: !0 })
      );
    }, this._onSelectTab = (e) => {
      this._activeTab = e;
    };
  }
  open() {
    this._isOpen = !0, this.updateComplete.then(() => this._dialog?.showModal());
  }
  close() {
    this._dialog?.close();
  }
  selectTab(e) {
    this._activeTab = e;
  }
  render() {
    if (!this._isOpen) return p;
    const e = this.zoneName || this.zone || "Comfort Band";
    return a`
      <dialog @close=${this._onClose}>
        <div class="frame">
          <header>
            <h2>${e}</h2>
            <button class="close" @click=${this.close} aria-label="Close">×</button>
          </header>
          <nav role="tablist">
            ${ai.map(
      (t) => a`
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
        return a`<comfort-band-now-tab
          .hass=${this.hass}
          .zone=${this.zone}
          .entities=${this.entities}
        ></comfort-band-now-tab>`;
      case "schedule":
        return a`<comfort-band-schedule-tab
          .hass=${this.hass}
          .zone=${this.zone}
        ></comfort-band-schedule-tab>`;
      case "profiles":
        return a`<comfort-band-profiles-tab .hass=${this.hass}></comfort-band-profiles-tab>`;
      case "insights":
        return a`<comfort-band-insights-tab
          .hass=${this.hass}
          .entities=${this.entities}
        ></comfort-band-insights-tab>`;
    }
  }
};
E.styles = [
  y,
  v`
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
k([
  l({ attribute: !1 })
], E.prototype, "hass", 2);
k([
  l({ type: String })
], E.prototype, "zone", 2);
k([
  l({ type: String })
], E.prototype, "zoneName", 2);
k([
  l({ attribute: !1 })
], E.prototype, "entities", 2);
k([
  m()
], E.prototype, "_activeTab", 2);
k([
  m()
], E.prototype, "_isOpen", 2);
k([
  at("dialog")
], E.prototype, "_dialog", 2);
E = k([
  $("comfort-band-modal")
], E);
var ci = Object.defineProperty, li = Object.getOwnPropertyDescriptor, At = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? li(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && ci(t, i, s), s;
};
let X = class extends b {
  constructor() {
    super(...arguments), this.config = {
      type: "custom:comfort-band-card",
      zone: ""
    }, this._onZoneChange = (e) => {
      const t = e.target.value;
      this._fireConfig({ ...this.config, zone: t });
    }, this._onCompactChange = (e) => {
      const t = e.target.checked, i = { ...this.config };
      t ? i.compact = !0 : delete i.compact, this._fireConfig(i);
    };
  }
  setConfig(e) {
    this.config = {
      type: e.type ?? "custom:comfort-band-card",
      zone: e.zone ?? "",
      ...e.compact !== void 0 ? { compact: e.compact } : {}
    };
  }
  _availableZones() {
    if (!this.hass) return [];
    const e = [];
    for (const t of Object.values(this.hass.devices))
      for (const [i, r] of t.identifiers)
        i === "comfort_band" && r.startsWith("zone:") && e.push(r.slice(5));
    return e.sort();
  }
  _fireConfig(e) {
    this.config = e, this.dispatchEvent(
      new CustomEvent("config-changed", {
        detail: { config: e },
        bubbles: !0,
        composed: !0
      })
    );
  }
  render() {
    const e = this._availableZones();
    return e.length === 0 ? a`<div class="empty">
        No Comfort Band zones found. Add one via Settings → Devices & Services first.
      </div>` : a`
      <label>
        Zone
        <select @change=${this._onZoneChange} .value=${this.config.zone || ""}>
          ${this.config.zone === "" ? a`<option value="" disabled selected>Select a zone…</option>` : null}
          ${e.map(
      (t) => a` <option value=${t} ?selected=${t === this.config.zone}>${t}</option> `
    )}
        </select>
      </label>
      <label class="checkbox-row">
        <input
          type="checkbox"
          .checked=${this.config.compact === !0}
          @change=${this._onCompactChange}
        />
        Compact mode (tile only, no expand on tap)
      </label>
    `;
  }
};
X.styles = [
  y,
  v`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-bottom: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
      }
      select,
      input[type='checkbox'] {
        font: inherit;
      }
      select {
        font-size: 14px;
        padding: 8px;
        border: 1px solid var(--divider-color, #cccccc);
        border-radius: 6px;
        color: var(--cb-text-primary);
        background: var(--ha-card-background, #ffffff);
      }
      .checkbox-row {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 14px;
        color: var(--cb-text-primary);
      }
      .empty {
        color: var(--cb-text-secondary);
        font-size: 13px;
      }
    `
];
At([
  l({ attribute: !1 })
], X.prototype, "hass", 2);
At([
  l({ attribute: !1 })
], X.prototype, "config", 2);
X = At([
  $("comfort-band-card-editor")
], X);
var hi = Object.defineProperty, di = Object.getOwnPropertyDescriptor, ht = (e, t, i, r) => {
  for (var s = r > 1 ? void 0 : r ? di(t, i) : t, o = e.length - 1, n; o >= 0; o--)
    (n = e[o]) && (s = (r ? n(t, i, s) : n(s)) || s);
  return r && s && hi(t, i, s), s;
};
let I = class extends b {
  constructor() {
    super(...arguments), this._onTileTap = () => {
      this._modal?.open();
    };
  }
  setConfig(e) {
    if (!e?.zone)
      throw new Error("comfort-band-card: `zone` is required");
    this._config = e;
  }
  /** HA's panel/grid uses this to size the card. ~1 row per ~50 px of content. */
  getCardSize() {
    return 2;
  }
  /** HA dashboard editor: tells HA which custom element to render as the
   *  visual config UI. Returning the element directly (not a string) is
   *  the modern API. */
  static getConfigElement() {
    return document.createElement("comfort-band-card-editor");
  }
  /** Default config seed when the user adds the card via "Add card" in the
   *  dashboard editor. Picks the first registered Comfort Band zone, if any. */
  static getStubConfig(e) {
    let t = "";
    if (e)
      for (const i of Object.values(e.devices)) {
        for (const [r, s] of i.identifiers)
          if (r === "comfort_band" && s.startsWith("zone:")) {
            t = s.slice(5);
            break;
          }
        if (t) break;
      }
    return { type: "custom:comfort-band-card", zone: t };
  }
  render() {
    if (!this._config || !this.hass) return a``;
    const e = this._config.zone, t = Be(this.hass, e);
    if (t.deviceId === null)
      return a`<div class="placeholder">
        Comfort Band zone <code>${e}</code> not found. Add it via Settings → Devices &
        Services.
      </div>`;
    const i = this._config.compact === !0, r = this._buildView(this.hass, t);
    return a`
      <comfort-band-tile
        zoneName=${r.zoneName}
        .roomTemp=${r.roomTemp}
        .low=${r.low}
        .high=${r.high}
        .action=${r.action}
        .overrideActive=${r.overrideActive}
        .overrideEnds=${r.overrideEnds}
        .noExpand=${i}
        @comfort-band-tile-tap=${this._onTileTap}
      ></comfort-band-tile>
      ${i ? null : a`<comfort-band-modal
            .hass=${this.hass}
            zone=${e}
            zoneName=${r.zoneName}
            .entities=${t}
          ></comfort-band-modal>`}
    `;
  }
  _buildView(e, t) {
    const i = (s) => s !== null ? e.states[s] : void 0, r = (s) => {
      const o = i(s);
      if (!o) return NaN;
      const n = parseFloat(o.state);
      return Number.isFinite(n) ? n : NaN;
    };
    return {
      zoneName: t.deviceName ?? this._config.zone,
      low: r(t.effectiveLow),
      high: r(t.effectiveHigh),
      roomTemp: r(t.roomTemperature),
      action: i(t.currentAction)?.state ?? "unknown",
      overrideActive: i(t.overrideActive)?.state === "on",
      overrideEnds: i(t.overrideEnds)?.state ?? null
    };
  }
};
I.styles = [
  y,
  v`
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
ht([
  l({ attribute: !1 })
], I.prototype, "hass", 2);
ht([
  m()
], I.prototype, "_config", 2);
ht([
  at("comfort-band-modal")
], I.prototype, "_modal", 2);
I = ht([
  $("comfort-band-card")
], I);
(window.customCards ??= []).push({
  type: "comfort-band-card",
  name: "Comfort Band",
  description: "Schedule editor and live status for a Comfort Band zone.",
  preview: !1
});
console.info(
  "%c COMFORT-BAND-CARD %c v0.1.0 ",
  "color:white;background:#2196F3;padding:2px 4px;border-radius:3px",
  "color:#000;background:#fff;padding:2px 4px;border-radius:3px"
);
