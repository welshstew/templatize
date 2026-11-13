/**
 * Conversion between UTF-16 code-unit offsets (JavaScript strings, DOM selections)
 * and Unicode code-point offsets (the API / Python str indices).
 *
 * Astral characters (emoji, some CJK) are 2 UTF-16 units but 1 code point. A lone
 * surrogate counts as one unit and one code point in both languages.
 */

function isHigh(c: number): boolean {
  return c >= 0xd800 && c <= 0xdbff;
}

function isLow(c: number): boolean {
  return c >= 0xdc00 && c <= 0xdfff;
}

export class OffsetMap {
  readonly text: string;
  /** code point index -> UTF-16 offset (length cpLength + 1) */
  private readonly cpToU16: Uint32Array;
  /** UTF-16 offset -> code point index (length text.length + 1) */
  private readonly u16ToCp: Uint32Array;
  readonly cpLength: number;
  /** true when every code point is one UTF-16 unit (fast path: offsets are equal) */
  readonly identity: boolean;

  constructor(text: string) {
    this.text = text;
    const n = text.length;
    const u16ToCp = new Uint32Array(n + 1);
    const cpToU16: number[] = [];
    let cp = 0;
    let i = 0;
    while (i < n) {
      cpToU16.push(i);
      u16ToCp[i] = cp;
      let width = 1;
      if (isHigh(text.charCodeAt(i)) && i + 1 < n && isLow(text.charCodeAt(i + 1))) {
        width = 2;
        u16ToCp[i + 1] = cp; // middle of a pair maps to the pair's code point
      }
      i += width;
      cp += 1;
    }
    cpToU16.push(n);
    u16ToCp[n] = cp;
    this.cpToU16 = Uint32Array.from(cpToU16);
    this.u16ToCp = u16ToCp;
    this.cpLength = cp;
    this.identity = cp === n;
  }

  toU16(cp: number): number {
    if (this.identity) return cp;
    const v = this.cpToU16[cp];
    if (v === undefined) throw new RangeError(`code point offset ${cp} out of range`);
    return v;
  }

  toCp(u16: number): number {
    if (this.identity) return u16;
    const v = this.u16ToCp[u16];
    if (v === undefined) throw new RangeError(`UTF-16 offset ${u16} out of range`);
    return v;
  }

  /** Is this UTF-16 offset between the two halves of a surrogate pair? */
  private midPair(u16: number): boolean {
    return (
      u16 > 0 &&
      u16 < this.text.length &&
      isHigh(this.text.charCodeAt(u16 - 1)) &&
      isLow(this.text.charCodeAt(u16))
    );
  }

  /** Convert a UTF-16 range to code points, widening it if it splits a surrogate pair. */
  rangeToCp(start: number, end: number): { start: number; end: number } {
    const s = this.midPair(start) ? start - 1 : start;
    const e = this.midPair(end) ? end + 1 : end;
    return { start: this.toCp(s), end: this.toCp(e) };
  }

  rangeToU16(start: number, end: number): { start: number; end: number } {
    return { start: this.toU16(start), end: this.toU16(end) };
  }
}
