/** @odoo-module **/

import { registry } from "@web/core/registry";
import { usePopover } from "@web/core/popover/popover_hook";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { ModelFieldSelectorPopover } from "@web/core/model_field_selector/model_field_selector_popover";
import { TextField, textField } from "@web/views/fields/text/text_field";

/**
 * The prompt field, with a button that inserts a `{{ field }}` reference.
 *
 * WHY THIS EXISTS
 *
 * The prompt language is deliberately not a template language: the only thing
 * between the braces is a field name, or a path through many2one links, and the
 * server refuses everything else rather than evaluating it
 * (`turnstone.automation._parse_field_path()`). That is the safety story — and
 * it leaves an administrator with one thing to guess: the field names of the
 * target model. This widget removes the guess.
 *
 * It offers the picker and writes only what the server accepts:
 *
 *   * a plain field of the target model, or a path through many2one links;
 *   * at most `MAX_PATH_SEGMENTS` segments, the same limit the server enforces,
 *     checked here so the picker cannot walk into a template that fails on save;
 *   * never *through* a one2many/many2many: such a field may still be the last
 *     segment (the server renders it as a list of names), but it has no single
 *     value to continue from;
 *   * never a binary field, which the server refuses because a file is not text.
 *
 * The picker appears only when the rule has a target model: with none there is
 * no model whose fields could be offered, and the prompt cannot use fields at
 * all — the server says so when the template is saved.
 */

// Mirrors `MAX_PATH_SEGMENTS` in models/turnstone_automation.py. A constant
// rather than a fetch, because it is part of the template language's
// definition and not a setting: if the server's limit moves, both move together
// and this comment is where to look.
const MAX_PATH_SEGMENTS = 4;

/**
 * Field types a prompt cannot hold.
 *
 * `binary` is refused by the renderer (a file is not text); `properties` is a
 * definition tree and `json` has no display value worth putting in an
 * instruction; `separator` is not a field; and a `many2one_reference` needs a
 * model the prompt language has no way to name.
 */
const UNUSABLE_TYPES = ["binary", "properties", "separator", "json", "many2one_reference"];

/**
 * The field picker, restricted to paths the prompt language accepts.
 *
 * A subclass rather than a filter function, because whether a field may be
 * *followed* is not a per-field question: it depends on how deep the path
 * already is, and `canFollowRelationFor()` is the one hook that sees both.
 */
export class PromptFieldSelectorPopover extends ModelFieldSelectorPopover {
    /** How many segments the current page's path already has. */
    get depth() {
        return (this.state.page?.path || "").split(".").filter(Boolean).length;
    }

    /**
     * Follow a link only when the result is still a legal path.
     *
     * Following a field from a page of depth `d` produces a page whose fields
     * would be segment `d + 2`, so the last follow allowed is from depth
     * `MAX_PATH_SEGMENTS - 2`. Only `many2one` gets a chevron: it is the only
     * relational type the server lets a path pass through.
     */
    canFollowRelationFor(fieldDef) {
        if (fieldDef.type !== "many2one") {
            return false;
        }
        return this.depth + 2 <= MAX_PATH_SEGMENTS;
    }
}

export class PromptTemplateField extends TextField {
    static template = "turnstone_integration.PromptTemplateField";
    static components = { ...TextField.components };

    setup() {
        super.setup();
        this.notification = useService("notification");
        this.fieldSelector = usePopover(PromptFieldSelectorPopover, {
            position: "bottom-start",
        });
        this.filterField = this.filterField.bind(this);
        this.applySelection = this.applySelection.bind(this);
    }

    /** The model whose fields the prompt may use, or "" when none is set. */
    get targetModel() {
        return this.props.record.data.target_model || "";
    }

    /** Offer a field only if the server would render it. */
    filterField(fieldDef) {
        return !UNUSABLE_TYPES.includes(fieldDef.type);
    }

    openFieldSelector(ev) {
        const resModel = this.targetModel;
        if (!resModel) {
            this.notification.add(
                _t(
                    "Pick a target model first: a prompt can only use fields of the model this rule watches."
                ),
                { type: "warning" }
            );
            return;
        }
        this.fieldSelector.open(ev.currentTarget, {
            resModel,
            filter: this.filterField,
            update: this.applySelection,
            showSearchInput: true,
            followRelations: true,
            isDebugMode: Boolean(this.env.debug),
        });
    }

    /**
     * `update` is the picker's "something was chosen" callback.
     *
     * It is called twice per interaction: with the path of every page the user
     * navigates to (one argument), and with the path *and* the field definition
     * when a field is actually selected (two arguments). Only the second is a
     * choice; inserting on the first would write `{{ partner_id }}` the moment
     * someone browsed into it.
     */
    applySelection(path, fieldDef) {
        if (fieldDef) {
            this.insertFieldPath(path);
        }
    }

    /**
     * Write `{{ path }}` at the caret and put the caret after it.
     *
     * The caret comes from the textarea when it still has one, and from the
     * position `TextField` remembered on blur otherwise: clicking the button
     * blurs the textarea, and inserting at the end because of that would be a
     * surprise on a twelve-line prompt.
     */
    insertFieldPath(path) {
        if (!path) {
            return;
        }
        const name = this.props.name;
        const current = this.props.record.data[name] || "";
        const textarea = this.textareaRef.el;
        let start = typeof this.selectionStart === "number" ? this.selectionStart : current.length;
        let end = start;
        if (textarea && textarea.selectionStart !== null) {
            start = textarea.selectionStart;
            end = textarea.selectionEnd ?? start;
        }
        // A caret remembered from before the value changed would split a word.
        start = Math.min(Math.max(start, 0), current.length);
        end = Math.min(Math.max(end, start), current.length);

        const reference = `{{ ${path} }}`;
        const next = current.slice(0, start) + reference + current.slice(end);
        this.props.record.update({ [name]: next });
        this.selectionStart = start + reference.length;
        if (textarea) {
            textarea.value = next;
            textarea.focus();
            textarea.setSelectionRange(this.selectionStart, this.selectionStart);
        }
    }
}

export const promptTemplateField = {
    ...textField,
    component: PromptTemplateField,
    // `o_field_text` is what aligns the button with the top of a multi-line
    // textarea rather than its middle (see web's char_field.scss).
    additionalClasses: [...(textField.additionalClasses || []), "o_field_text"],
};

registry.category("fields").add("turnstone_prompt_template", promptTemplateField);
