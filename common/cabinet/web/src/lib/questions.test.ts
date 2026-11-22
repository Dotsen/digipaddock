import { describe, expect, it } from "vitest";
import { emptyAnswers, isShort, missingRequired, summary } from "./questions";
import { FORM, LONG } from "./questions.samples";
import type { FormAnswer } from "./types";

describe("questions", () => {
  it("empty answers cover every question", () => {
    expect(emptyAnswers(FORM)).toEqual({
      method: { selected_option_ids: [], custom_text: "" },
      extra: { selected_option_ids: [], custom_text: "" },
      details: { selected_option_ids: [], custom_text: "" },
    });
  });

  it("a required question is met by a choice or by own words, never by blanks", () => {
    const answers = emptyAnswers(FORM);
    expect(missingRequired(FORM, answers)).toEqual(["method", "details"]);
    answers.method.selected_option_ids = ["ssh"];
    answers.details.custom_text = "   ";
    expect(missingRequired(FORM, answers)).toEqual(["details"]);
    answers.method = { selected_option_ids: [], custom_text: "свой путь" };
    answers.details.custom_text = "срок неделя";
    expect(missingRequired(FORM, answers)).toEqual([]);
  });

  it("the summary takes labels from the form's snapshot; no answer is empty", () => {
    const answer: FormAnswer = {
      schema_version: 1,
      origin: "agent",
      form_sha256: "x",
      answers: {
        method: { selected_option_ids: ["ssh"], custom_text: "" },
        extra: { selected_option_ids: ["queue", "worker"], custom_text: "и задержку" },
      },
      comment: "",
    };
    expect(summary(FORM, answer)).toEqual([
      { title: "Как проверять?", labels: ["SSH"], custom: "" },
      { title: "Что ещё?", labels: ["Обработчик", "Очередь"], custom: "и задержку" },
      { title: "Условия", labels: [], custom: "" },
    ]);
  });

  it("up to three questions is one list, more are steps", () => {
    expect(isShort(FORM)).toBe(true);
    expect(isShort(LONG)).toBe(false);
  });
});
