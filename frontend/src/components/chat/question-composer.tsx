"use client"

import { useState, type FormEvent, type KeyboardEvent } from "react"
import { ScanSearchIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import { Button } from "@/components/ui/button"
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field"
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupText,
  InputGroupTextarea,
} from "@/components/ui/input-group"
import { Marker, MarkerContent } from "@/components/ui/marker"
import { Spinner } from "@/components/ui/spinner"

interface QuestionComposerProps {
  pending: boolean
  onReveal: (question: string) => boolean
}

export function QuestionComposer({ pending, onReveal }: QuestionComposerProps) {
  const [draft, setDraft] = useState("")
  const { t } = useLocale()
  const suggestedPrompts = [
    t("chat.composer.suggestionOne"),
    t("chat.composer.suggestionTwo"),
    t("chat.composer.suggestionThree"),
  ]

  function revealDraft() {
    if (onReveal(draft)) setDraft("")
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    revealDraft()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.nativeEvent.isComposing) return
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault()
      revealDraft()
    }
  }

  return (
    <section className="question-placement-stage" aria-labelledby="question-placement-title">
      <Marker variant="separator" className="question-placement-marker">
        <MarkerContent>{t("chat.composer.marker")}</MarkerContent>
      </Marker>

      <div className="prompt-index-rail scroll-fade-x" aria-label={t("chat.composer.suggestions")}>
        {suggestedPrompts.map((prompt, index) => (
          <Button
            key={prompt}
            type="button"
            variant="ghost"
            size="sm"
            className="prompt-index-card-v03"
            disabled={pending}
            onClick={() => setDraft(prompt)}
          >
            <span className="prompt-card-corner" aria-hidden="true">
              {index + 1}<b>♠</b>
            </span>
            {prompt}
          </Button>
        ))}
      </div>

      <form onSubmit={onSubmit} className="question-composer-v03">
        <Field data-disabled={pending || undefined}>
          <FieldLabel htmlFor="magic-question" className="sr-only">
            {t("chat.composer.label")}
          </FieldLabel>
          <InputGroup className="question-instrument-card">
            <InputGroupAddon align="block-start" className="question-instrument-heading">
              <span className="question-card-index" aria-hidden="true">Q♠</span>
              <span>
                <b id="question-placement-title">{t("chat.composer.title")}</b>
                <small>{t("chat.composer.subtitle")}</small>
              </span>
              <i aria-hidden="true">MF / Q-01</i>
            </InputGroupAddon>
            <InputGroupTextarea
              id="magic-question"
              name="question"
              autoComplete="off"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              placeholder={t("chat.composer.placeholder")}
              rows={4}
              disabled={pending}
              aria-describedby="magic-question-hint"
              className="question-instrument-input"
            />
            <InputGroupAddon align="block-end" className="question-instrument-footer">
              <InputGroupText id="magic-question-hint">
                {t("chat.composer.keyboardHint")}
              </InputGroupText>
              <InputGroupButton
                type="submit"
                variant="outline"
                size="sm"
                className="question-instrument-action"
                disabled={!draft.trim() || pending}
              >
                {pending ? (
                  <Spinner data-icon="inline-start" />
                ) : (
                  <ScanSearchIcon data-icon="inline-start" aria-hidden="true" />
                )}
                {pending ? t("chat.composer.studying") : t("chat.composer.submit")}
              </InputGroupButton>
            </InputGroupAddon>
          </InputGroup>
          <FieldDescription className="sr-only">
            {t("chat.composer.description")}
          </FieldDescription>
        </Field>
      </form>
    </section>
  )
}
