/** @odoo-module **/

import { markup } from "@odoo/owl";

/**
 * A small Markdown renderer for agent output.
 *
 * SAFETY MODEL: escape first, then build.
 *
 * Every character of agent text is HTML-escaped before any transform runs, so
 * no tag in the source can survive into the output. The only markup in the
 * result is markup this file wrote. DOMPurify then runs as a second,
 * independent layer -- belt and braces, because this text is ultimately written
 * by a model that reads untrusted input (pod logs, email bodies, web pages),
 * and "the agent would never emit a script tag" is not a security argument.
 *
 * Deliberately not a complete CommonMark implementation. It covers what agents
 * actually emit -- headings, fences, lists, tables, emphasis, links -- and
 * leaves anything else as literal text, which is the safe failure mode.
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const CODE_OPEN = "%%TSCODE";
const CODE_CLOSE = "%%";

function esc(text) {
    return String(text ?? "").replace(/[&<>"']/g, (c) => ESCAPES[c]);
}

/** Only http(s) and mailto survive. Blocks javascript: and data: URLs. */
function safeHref(url) {
    const trimmed = String(url || "").trim();
    if (/^(https?:\/\/|mailto:)/i.test(trimmed)) {
        return trimmed;
    }
    return null;
}

/** Inline constructs, applied to already-escaped text. */
function inline(escaped) {
    let out = escaped;

    // Code spans first: their content must not be touched by later rules, so
    // they are swapped for placeholders and restored at the end.
    const codes = [];
    out = out.replace(/`([^`\n]+)`/g, (_m, code) => {
        codes.push(code);
        return CODE_OPEN + (codes.length - 1) + CODE_CLOSE;
    });

    // [text](url). The url group tolerates one level of nested parentheses so
    // that a rejected link consumes its whole "(...)" rather than leaving a
    // stray ")" behind -- which is what javascript:alert(1) used to do.
    out = out.replace(/\[([^\]\n]+)\]\(([^()\s]*(?:\([^()]*\)[^()\s]*)*)(?:\s+&quot;[^&]*&quot;)?\)/g, (m, text, url) => {
        // The url arrives escaped; unescape just enough to validate the scheme.
        const raw = url.replace(/&amp;/g, "&").replace(/&#39;/g, "'").replace(/&quot;/g, '"');
        const href = safeHref(raw);
        if (!href) {
            return text; // drop the link, keep the words
        }
        return '<a href="' + esc(href) + '" target="_blank" rel="noopener noreferrer">' + text + "</a>";
    });

    // Bare URLs that were not already linked.
    out = out.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, (m, lead, url) => {
        return lead + '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + url + "</a>";
    });

    out = out
        .replace(/\*\*\*([^*\n]+)\*\*\*/g, "<strong><em>$1</em></strong>")
        .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
        .replace(/(^|\W)__([^_\n]+)__(?=\W|$)/g, "$1<strong>$2</strong>")
        .replace(/(^|[^*\w])\*([^*\n]+)\*(?=[^*\w]|$)/g, "$1<em>$2</em>")
        .replace(/(^|\W)_([^_\n]+)_(?=\W|$)/g, "$1<em>$2</em>")
        .replace(/~~([^~\n]+)~~/g, "<del>$1</del>");

    const codeRe = new RegExp(CODE_OPEN.replace(/%/g, "%") + "(\\d+)" + CODE_CLOSE, "g");
    out = out.replace(codeRe, (_m, i) => "<code>" + codes[Number(i)] + "</code>");
    return out;
}

function tableRow(line) {
    return line
        .trim()
        .replace(/^\||\|$/g, "")
        .split("|")
        .map((c) => c.trim());
}

const isTableDivider = (line) =>
    /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes("-");

/**
 * @param {string} text raw agent output
 * @returns {ReturnType<markup>} safe HTML for t-out
 */
export function renderMarkdown(text) {
    const src = String(text ?? "");
    const lines = src.split("\n");
    const html = [];
    let i = 0;

    const listStack = []; // "ul" | "ol"
    const closeLists = (toDepth = 0) => {
        while (listStack.length > toDepth) {
            html.push("</" + listStack.pop() + ">");
        }
    };

    while (i < lines.length) {
        const line = lines[i];

        // Fenced code block
        const fence = line.match(/^\s*```+\s*([\w+-]*)\s*$/);
        if (fence) {
            closeLists();
            const lang = fence[1] || "";
            const body = [];
            i++;
            while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) {
                body.push(lines[i]);
                i++;
            }
            i++; // closing fence
            const cls = lang ? ' class="language-' + esc(lang) + '"' : "";
            html.push('<pre class="o_ts_code"><code' + cls + ">" + esc(body.join("\n")) + "</code></pre>");
            continue;
        }

        // Table: a header row followed by a divider
        if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
            closeLists();
            const header = tableRow(line);
            i += 2;
            const rows = [];
            while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
                rows.push(tableRow(lines[i]));
                i++;
            }
            html.push('<div class="o_ts_table_wrap"><table class="table table-sm o_ts_table">');
            html.push("<thead><tr>");
            header.forEach((c) => html.push("<th>" + inline(esc(c)) + "</th>"));
            html.push("</tr></thead><tbody>");
            rows.forEach((r) => {
                html.push("<tr>");
                r.forEach((c) => html.push("<td>" + inline(esc(c)) + "</td>"));
                html.push("</tr>");
            });
            html.push("</tbody></table></div>");
            continue;
        }

        // Heading. h1 becomes h3 so agent output never outranks Odoo chrome.
        const heading = line.match(/^(#{1,6})\s+(.*)$/);
        if (heading) {
            closeLists();
            const level = Math.min(heading[1].length + 2, 6);
            html.push("<h" + level + ' class="o_ts_h">' + inline(esc(heading[2])) + "</h" + level + ">");
            i++;
            continue;
        }

        // Horizontal rule
        if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
            closeLists();
            html.push("<hr/>");
            i++;
            continue;
        }

        // Blockquote
        if (/^\s*>\s?/.test(line)) {
            closeLists();
            const quoted = [];
            while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
                quoted.push(lines[i].replace(/^\s*>\s?/, ""));
                i++;
            }
            html.push('<blockquote class="o_ts_quote">' + inline(esc(quoted.join(" "))) + "</blockquote>");
            continue;
        }

        // List item (nesting by indent, two spaces per level)
        const li = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (li) {
            const depth = Math.floor(li[1].replace(/\t/g, "  ").length / 2) + 1;
            const type = /\d/.test(li[2]) ? "ol" : "ul";
            while (listStack.length > depth) {
                html.push("</" + listStack.pop() + ">");
            }
            if (listStack.length === depth && listStack[depth - 1] !== type) {
                html.push("</" + listStack.pop() + ">");
            }
            while (listStack.length < depth) {
                listStack.push(type);
                html.push("<" + type + ">");
            }
            html.push("<li>" + inline(esc(li[3])) + "</li>");
            i++;
            continue;
        }

        // Blank line
        if (!line.trim()) {
            closeLists();
            i++;
            continue;
        }

        // Paragraph: consume until a blank line or a construct starts
        closeLists();
        const para = [];
        while (
            i < lines.length &&
            lines[i].trim() &&
            !/^\s*```/.test(lines[i]) &&
            !/^(#{1,6})\s/.test(lines[i]) &&
            !/^\s*>\s?/.test(lines[i]) &&
            !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) &&
            !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])
        ) {
            para.push(lines[i]);
            i++;
        }
        // Single newlines inside a paragraph are meaningful in agent output
        // (wrapped prose, aligned values), so keep them as breaks.
        html.push("<p>" + inline(esc(para.join("\n"))).replace(/\n/g, "<br/>") + "</p>");
    }
    closeLists();

    let out = html.join("");

    // Second layer. Odoo bundles DOMPurify in web.assets_backend; if it is ever
    // absent the escape-first construction above still holds on its own.
    const purify = globalThis.DOMPurify;
    if (purify && typeof purify.sanitize === "function") {
        out = purify.sanitize(out, {
            ALLOWED_TAGS: [
                "p", "br", "strong", "em", "del", "code", "pre", "a", "ul", "ol", "li",
                "blockquote", "h3", "h4", "h5", "h6", "hr", "table", "thead", "tbody",
                "tr", "th", "td", "div", "span",
            ],
            ALLOWED_ATTR: ["href", "target", "rel", "class"],
            ALLOW_DATA_ATTR: false,
        });
    }
    return markup(out);
}
