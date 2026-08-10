"use client"

import { BookOpenTextIcon, LampDeskIcon, SpadeIcon } from "lucide-react"

import { useLocale } from "@/components/i18n/locale-provider"
import type { MessageKey } from "@/lib/i18n/messages"

const revealRegister = [
  { number: "I", labelKey: "chat.header.effect" },
  { number: "II", labelKey: "chat.header.structure" },
  { number: "III", labelKey: "chat.header.cognition" },
  { number: "IV", labelKey: "chat.header.evidence" },
] as const satisfies ReadonlyArray<{ number: string; labelKey: MessageKey }>

export function WorkshopHeader() {
  const { t } = useLocale()

  return (
    <header className="chat-workshop-header chat-private-instrument" aria-labelledby="chat-workshop-title">
      <div className="chat-workshop-light" aria-hidden="true" />
      <div className="private-instrument-identity">
        <div className="chat-brass-plate">
          <LampDeskIcon aria-hidden="true" />
          {t("chat.header.apparatus")}
        </div>
        <p className="chat-backstage-note">{t("chat.header.room")}</p>
        <h1 id="chat-workshop-title">{t("chat.header.title")}</h1>
        <p className="chat-workshop-intro">
          {t("chat.header.intro")}
        </p>
      </div>

      <div className="private-instrument-register">
        <div className="register-heading">
          <BookOpenTextIcon aria-hidden="true" />
          <span>{t("chat.header.register")}</span>
        </div>
        <ol aria-label={t("chat.header.revealLayers")}>
          {revealRegister.map((item) => (
            <li key={item.number}>
              <b>{item.number}</b>
              <span>{t(item.labelKey)}</span>
            </li>
          ))}
        </ol>
      </div>

      <div className="chat-card-stack private-card-stack" aria-hidden="true">
        <span className="chat-card-back" />
        <span className="chat-card-face"><SpadeIcon /></span>
        <i className="card-brass-clip" />
      </div>
    </header>
  )
}
