import { WorkshopHeader } from "@/components/chat/workshop-header"
import { ChatWorkspace } from "@/features/chat/chat-workspace"

export default function ChatPage() {
  return (
    <div className="module-stage" data-module="chat">
      <WorkshopHeader />
      <ChatWorkspace />
    </div>
  )
}
