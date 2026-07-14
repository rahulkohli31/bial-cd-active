"use client";;
import {
 ComposerAddAttachment,
 ComposerAttachments,
 UserMessageAttachments,
} from"@/components/attachment";
import { ThreadFollowupSuggestions } from"@/components/follow-up-suggestions";
import { DotMatrix } from"@/components/dot-matrix";
import { MarkdownText } from"@/components/markdown-text";
import {
 Reasoning,
 ReasoningContent,
 ReasoningRoot,
 ReasoningText,
 ReasoningTrigger,
} from"@/components/reasoning";
import { ToolFallback } from"@/components/tool-fallback";
import {
 ToolGroupContent,
 ToolGroupRoot,
 ToolGroupTrigger,
} from"@/components/tool-group";
import { TooltipIconButton } from"@/components/tooltip-icon-button";
import { Button } from"@/components/ui/button";
import { cn } from"@/lib/utils";
import {
 ActionBarMorePrimitive,
 ActionBarPrimitive,
 AuiIf,
 BranchPickerPrimitive,
 ComposerPrimitive,
 ErrorPrimitive,
 groupPartByType,
 MessagePrimitive,
 SuggestionPrimitive,
 ThreadPrimitive,
 useAuiState,
} from"@assistant-ui/react";
import {
 ArrowDownIcon,
 ArrowUpIcon,
 CheckIcon,
 ChevronLeftIcon,
 ChevronRightIcon,
 CopyIcon,
 DownloadIcon,
 MicIcon,
 MoreHorizontalIcon,
 PencilIcon,
 RefreshCwIcon,
 SquareIcon,
} from"lucide-react";
import { createContext, useContext } from"react";

const EMPTY_COMPONENTS = {};

const ThreadComponentsContext =
 createContext(EMPTY_COMPONENTS);

// Startup exposes a loading placeholder thread; treat it as a new chat so
// the composer mounts centered. Loads after startup keep the docked layout.
const isNewChatView = (s) =>
 s.thread.messages.length === 0 &&
 (!s.thread.isLoading || s.threads.isLoading);

export const Thread = ({ components = EMPTY_COMPONENTS }) => {
 const isEmpty = useAuiState(isNewChatView);

 return (
 <ThreadComponentsContext.Provider value={components}>
 <ThreadRoot isEmpty={isEmpty} />
 </ThreadComponentsContext.Provider>
 );
};

const ThreadRoot = ({ isEmpty }) => {
 const { Welcome = ThreadWelcome } = useContext(ThreadComponentsContext);

 return (
 <ThreadPrimitive.Root
 className="aui-root aui-thread-root bg-white @container flex h-full flex-col"
 style={{
 ["--thread-max-width"]:"44rem",
 ["--composer-bg"]:
"#F8F9FA",
 ["--composer-radius"]:"1.5rem",
 ["--composer-padding"]:"8px",
 }}>
 <ThreadPrimitive.Viewport
 turnAnchor="top"
 data-slot="aui_thread-viewport"
 className="relative flex flex-1 flex-col overflow-x-auto overflow-y-scroll scroll-smooth">
 <div
 className={cn(
"mx-auto flex w-full max-w-(--thread-max-width) flex-1 flex-col px-4 pt-4",
 isEmpty &&"justify-center"
 )}>
 <AuiIf condition={isNewChatView}>
 <Welcome />
 </AuiIf>

 <div
 data-slot="aui_message-group"
 className="mb-14 flex flex-col gap-y-6 empty:hidden">
 <ThreadPrimitive.Messages>
 {() => <ThreadMessage />}
 </ThreadPrimitive.Messages>
 </div>

 <ThreadPrimitive.ViewportFooter
 className={cn(
"aui-thread-viewport-footer bg-white flex flex-col gap-4 overflow-visible pb-4 md:pb-6",
 !isEmpty &&
"sticky bottom-0 mt-auto rounded-t-(--composer-radius)"
 )}>
 <ThreadScrollToBottom />
 <ThreadFollowupSuggestions />
 <Composer />
 <AuiIf condition={(s) => isNewChatView(s) && s.composer.isEmpty}>
 <ThreadSuggestions />
 </AuiIf>
 </ThreadPrimitive.ViewportFooter>
 </div>
 </ThreadPrimitive.Viewport>
 </ThreadPrimitive.Root>
 );
};

const ThreadMessage = () => {
 const { AssistantMessage: AssistantMessageComponent = AssistantMessage } =
 useContext(ThreadComponentsContext);
 const role = useAuiState((s) => s.message.role);
 const isEditing = useAuiState((s) => s.message.composer.isEditing);

 if (isEditing) return <EditComposer />;
 if (role ==="user") return <UserMessage />;
 return <AssistantMessageComponent />;
};

const ThreadScrollToBottom = () => {
 return (
 <ThreadPrimitive.ScrollToBottom asChild>
 <TooltipIconButton
 tooltip="Scroll to bottom"
 variant="outline"
 className="aui-thread-scroll-to-bottom absolute -top-12 z-10 self-center rounded-full p-4 disabled:invisible">
 <ArrowDownIcon />
 </TooltipIconButton>
 </ThreadPrimitive.ScrollToBottom>
 );
};

const ThreadWelcome = () => {
 return (
 <div
 className="aui-thread-welcome-root mb-6 flex flex-col items-center px-4 text-center">
 <h1
 className="aui-thread-welcome-message-inner fade-in slide-in-from-bottom-1 animate-in fill-mode-both text-2xl font-semibold duration-200">
 Plan your next app
 </h1>
 </div>
 );
};

const ThreadSuggestions = () => {
 return (
 <div
 className="aui-thread-welcome-suggestions flex w-full flex-wrap items-center justify-center gap-2 px-4">
 <ThreadPrimitive.Suggestions>
 {() => <ThreadSuggestionItem />}
 </ThreadPrimitive.Suggestions>
 </div>
 );
};

const ThreadSuggestionItem = () => {
 return (
 <div
 className="aui-thread-welcome-suggestion-display fade-in slide-in-from-bottom-2 animate-in fill-mode-both duration-200">
 <SuggestionPrimitive.Trigger send asChild>
 <Button
 variant="ghost"
 className="aui-thread-welcome-suggestion text-tertiary hover:bg-white border-surface-muted h-auto gap-1.5 rounded-full border border-surface-muted px-3.5 py-1.5 text-sm font-normal whitespace-nowrap transition-colors">
 <SuggestionPrimitive.Title className="aui-thread-welcome-suggestion-text-1"/>
 <SuggestionPrimitive.Description className="aui-thread-welcome-suggestion-text-2 empty:hidden"/>
 </Button>
 </SuggestionPrimitive.Trigger>
 </div>
 );
};

const Composer = () => {
 return (
 <ComposerPrimitive.Root className="aui-composer-root relative flex w-full flex-col">
 <ComposerPrimitive.AttachmentDropzone asChild>
 <div
 data-slot="aui_composer-shell"
 className="border-surface-muted data-[dragging=true]:border-bial-border focus-within:border-surface-muted flex w-full flex-col gap-2 rounded-(--composer-radius) border border-surface-muted bg-(--composer-bg) p-(--composer-padding) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] transition-[border-color,box-shadow] focus-within:shadow-[0_6px_24px_-8px_rgba(0,0,0,0.12),0_1px_2px_rgba(0,0,0,0.05)] data-[dragging=true]:border-dashed data-[dragging=true]:bg-[#FFF4E0] dark:shadow-none">
 <ComposerAttachments />
 <ComposerPrimitive.Input
 placeholder="Describe what you're thinking… (Shift+Enter for new line)"
 className="aui-composer-input caret-primary placeholder:text-neutral max-h-32 min-h-10 w-full resize-none bg-transparent px-2.5 py-1 text-base outline-none"
 rows={1}
 autoFocus
 enterKeyHint="send"
 aria-label="Message input"/>
 <ComposerAction />
 </div>
 </ComposerPrimitive.AttachmentDropzone>
 </ComposerPrimitive.Root>
 );
};

const ComposerAction = () => {
 return (
 <div
 className="aui-composer-action-wrapper relative flex items-center justify-between">
 <ComposerAddAttachment />
 <div className="flex items-center gap-1.5">
 <AuiIf condition={(s) => s.thread.capabilities.dictation}>
 <AuiIf condition={(s) => s.composer.dictation == null}>
 <ComposerPrimitive.Dictate asChild>
 <TooltipIconButton
 tooltip="Voice input"
 side="bottom"
 type="button"
 variant="ghost"
 size="icon"
 className="aui-composer-dictate size-7 rounded-full"
 aria-label="Start voice input">
 <MicIcon className="aui-composer-dictate-icon size-4"/>
 </TooltipIconButton>
 </ComposerPrimitive.Dictate>
 </AuiIf>
 <AuiIf condition={(s) => s.composer.dictation != null}>
 <ComposerPrimitive.StopDictation asChild>
 <TooltipIconButton
 tooltip="Stop dictation"
 side="bottom"
 type="button"
 variant="ghost"
 size="icon"
 className="aui-composer-stop-dictation text-danger size-7 rounded-full"
 aria-label="Stop voice input">
 <SquareIcon
 className="aui-composer-stop-dictation-icon size-3.5 animate-pulse fill-current"/>
 </TooltipIconButton>
 </ComposerPrimitive.StopDictation>
 </AuiIf>
 </AuiIf>
 <AuiIf condition={(s) => !s.thread.isRunning}>
 <ComposerPrimitive.Send asChild>
 <TooltipIconButton
 tooltip="Send message"
 side="bottom"
 type="button"
 variant="default"
 size="icon"
 className="aui-composer-send size-7 rounded-full bg-secondary hover:bg-secondary-600"
 aria-label="Send message">
 <ArrowUpIcon className="aui-composer-send-icon size-4.5"/>
 </TooltipIconButton>
 </ComposerPrimitive.Send>
 </AuiIf>
 <AuiIf condition={(s) => s.thread.isRunning}>
 <ComposerPrimitive.Cancel asChild>
 <Button
 type="button"
 variant="default"
 size="icon"
 className="aui-composer-cancel size-7 rounded-full"
 aria-label="Stop generating">
 <SquareIcon className="aui-composer-cancel-icon size-3.5 fill-current"/>
 </Button>
 </ComposerPrimitive.Cancel>
 </AuiIf>
 </div>
 </div>
 );
};

const MessageError = () => {
 return (
 <MessagePrimitive.Error>
 <ErrorPrimitive.Root
 className="aui-message-error-root border-danger bg-danger text-danger mt-2 rounded-md border border-surface-muted p-3 text-sm dark:text-red-200">
 <ErrorPrimitive.Message className="aui-message-error-message line-clamp-2"/>
 </ErrorPrimitive.Root>
 </MessagePrimitive.Error>
 );
};

const AssistantMessage = () => {
 const {
 ToolFallback: ToolFallbackComponent = ToolFallback,
 ToolGroup,
 ReasoningGroup,
 } = useContext(ThreadComponentsContext);

 const ACTION_BAR_PT ="pt-1.5";
 // Keep the action bar inside the contained root's paint box, then cancel its reserved space in flow.
 const ACTION_BAR_HEIGHT = `min-h-7.5 ${ACTION_BAR_PT}`;

 return (
 <MessagePrimitive.Root
 data-slot="aui_assistant-message-root"
 data-role="assistant"
 className="fade-in slide-in-from-bottom-1 animate-in relative -mb-7.5 pb-7.5 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto]">
 <div
 data-slot="aui_assistant-message-content"
 className="text-tertiary px-2 leading-relaxed wrap-break-word">
 <MessagePrimitive.GroupedParts
 groupBy={groupPartByType({
 reasoning: ["group-chainOfThought","group-reasoning"],
"tool-call": ["group-chainOfThought","group-tool"],
"standalone-tool-call": [],
 })}>
 {({ part, children }) => {
 switch (part.type) {
 case"group-chainOfThought":
 return <div data-slot="aui_chain-of-thought">{children}</div>;
 case"group-tool":
 if (ToolGroup) {
 return <ToolGroup group={part}>{children}</ToolGroup>;
 }
 return (
 <ToolGroupRoot variant="ghost">
 <ToolGroupTrigger count={part.indices.length} active={part.status.type ==="running"} />
 <ToolGroupContent>{children}</ToolGroupContent>
 </ToolGroupRoot>
 );
 case"group-reasoning": {
 if (ReasoningGroup) {
 return (<ReasoningGroup group={part}>{children}</ReasoningGroup>);
 }
 const running = part.status.type ==="running";
 return (
 <ReasoningRoot streaming={running}>
 <ReasoningTrigger active={running} />
 <ReasoningContent aria-busy={running}>
 <ReasoningText>{children}</ReasoningText>
 </ReasoningContent>
 </ReasoningRoot>
 );
 }
 case"text":
 return <MarkdownText />;
 case"reasoning":
 return <Reasoning {...part} />;
 case"tool-call":
 return part.toolUI ?? <ToolFallbackComponent {...part} />;
 case"data":
 return part.dataRendererUI;
 case"indicator":
 return (
 <span data-slot="aui_assistant-message-indicator" className="inline-flex items-center text-primary">
 <DotMatrix state="thinking" label="Assistant is working" />
 </span>
 );
 default:
 return null;
 }
 }}
 </MessagePrimitive.GroupedParts>
 <MessageError />
 </div>
 <div
 data-slot="aui_assistant-message-footer"
 className={cn("ms-2 flex items-center", ACTION_BAR_HEIGHT)}>
 <BranchPicker />
 <AssistantActionBar />
 </div>
 </MessagePrimitive.Root>
 );
};

const AssistantActionBar = () => {
 return (
 <ActionBarPrimitive.Root
 hideWhenRunning
 autohide="not-last"
 className="aui-assistant-action-bar-root text-neutral animate-in fade-in col-start-3 row-start-2 -ms-1 flex gap-1 duration-200">
 <ActionBarPrimitive.Copy asChild>
 <TooltipIconButton tooltip="Copy">
 <AuiIf condition={(s) => s.message.isCopied}>
 <CheckIcon className="animate-in zoom-in-50 fade-in duration-200 ease-out"/>
 </AuiIf>
 <AuiIf condition={(s) => !s.message.isCopied}>
 <CopyIcon className="animate-in zoom-in-75 fade-in duration-150"/>
 </AuiIf>
 </TooltipIconButton>
 </ActionBarPrimitive.Copy>
 <ActionBarPrimitive.Reload asChild>
 <TooltipIconButton tooltip="Refresh">
 <RefreshCwIcon />
 </TooltipIconButton>
 </ActionBarPrimitive.Reload>
 <ActionBarMorePrimitive.Root>
 <ActionBarMorePrimitive.Trigger asChild>
 <TooltipIconButton
 tooltip="More"
 className="data-[state=open]:bg-white">
 <MoreHorizontalIcon />
 </TooltipIconButton>
 </ActionBarMorePrimitive.Trigger>
 <ActionBarMorePrimitive.Content
 side="bottom"
 align="start"
 sideOffset={6}
 className="aui-action-bar-more-content bg-white text-tertiary data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=open]:animate-in data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[state=closed]:animate-out data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 z-50 min-w-[8rem] overflow-hidden rounded-xl border border-surface-muted p-1.5 shadow-lg backdrop-blur-sm">
 <ActionBarPrimitive.ExportMarkdown asChild>
 <ActionBarMorePrimitive.Item
 className="aui-action-bar-more-item hover:bg-white hover:text-tertiary focus:bg-white focus:text-tertiary flex cursor-pointer items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm outline-none select-none">
 <DownloadIcon className="size-4"/>
 Export as Markdown
 </ActionBarMorePrimitive.Item>
 </ActionBarPrimitive.ExportMarkdown>
 </ActionBarMorePrimitive.Content>
 </ActionBarMorePrimitive.Root>
 </ActionBarPrimitive.Root>
 );
};

const UserMessage = () => {
 return (
 <MessagePrimitive.Root
 data-slot="aui_user-message-root"
 className="fade-in slide-in-from-bottom-1 animate-in grid auto-rows-auto grid-cols-[minmax(72px,1fr)_auto] content-start gap-y-2 px-2 duration-150 [contain-intrinsic-size:auto_200px] [content-visibility:auto] [&:where(>*)]:col-start-2"
 data-role="user">
 <UserMessageAttachments />
 <div className="aui-user-message-content-wrapper relative col-start-2 min-w-0">
 <div
 className="aui-user-message-content peer bg-tertiary text-white rounded-xl px-4 py-2 wrap-break-word empty:hidden">
 <MessagePrimitive.Parts />
 </div>
 <div
 className="aui-user-action-bar-wrapper absolute start-0 top-1/2 -translate-x-full -translate-y-1/2 pe-2 peer-empty:hidden rtl:translate-x-full">
 <UserActionBar />
 </div>
 </div>
 <BranchPicker
 data-slot="aui_user-branch-picker"
 className="col-span-full col-start-1 row-start-3 -me-1 justify-end"/>
 </MessagePrimitive.Root>
 );
};

const UserActionBar = () => {
 return (
 <ActionBarPrimitive.Root
 hideWhenRunning
 autohide="not-last"
 className="aui-user-action-bar-root flex flex-col items-end">
 <ActionBarPrimitive.Edit asChild>
 <TooltipIconButton tooltip="Edit"className="aui-user-action-edit">
 <PencilIcon />
 </TooltipIconButton>
 </ActionBarPrimitive.Edit>
 </ActionBarPrimitive.Root>
 );
};

const EditComposer = () => {
 return (
 <MessagePrimitive.Root
 data-slot="aui_edit-composer-wrapper"
 className="flex flex-col px-2 [contain-intrinsic-size:auto_200px] [content-visibility:auto]">
 <ComposerPrimitive.Root
 className="aui-edit-composer-root border-surface-muted ms-auto flex w-full max-w-[85%] flex-col rounded-(--composer-radius) border border-surface-muted bg-(--composer-bg) shadow-[0_4px_16px_-8px_rgba(0,0,0,0.08),0_1px_2px_rgba(0,0,0,0.04)] dark:shadow-none">
 <ComposerPrimitive.Input
 className="aui-edit-composer-input text-tertiary min-h-14 w-full resize-none bg-transparent px-4 pt-3 pb-1 text-base outline-none"
 autoFocus />
 <div
 className="aui-edit-composer-footer mx-2.5 mb-2.5 flex items-center gap-1.5 self-end">
 <ComposerPrimitive.Cancel asChild>
 <Button variant="ghost"size="sm"className="h-8 rounded-full px-3.5">
 Cancel
 </Button>
 </ComposerPrimitive.Cancel>
 <ComposerPrimitive.Send asChild>
 <Button size="sm"className="h-8 rounded-full px-3.5">
 Update
 </Button>
 </ComposerPrimitive.Send>
 </div>
 </ComposerPrimitive.Root>
 </MessagePrimitive.Root>
 );
};

const BranchPicker = ({
 className,
 ...rest
}) => {
 return (
 <BranchPickerPrimitive.Root
 hideWhenSingleBranch
 className={cn(
"aui-branch-picker-root text-neutral -ms-2 me-2 inline-flex items-center text-xs",
 className
 )}
 {...rest}>
 <BranchPickerPrimitive.Previous asChild>
 <TooltipIconButton tooltip="Previous">
 <ChevronLeftIcon />
 </TooltipIconButton>
 </BranchPickerPrimitive.Previous>
 <span className="aui-branch-picker-state font-medium">
 <BranchPickerPrimitive.Number /> / <BranchPickerPrimitive.Count />
 </span>
 <BranchPickerPrimitive.Next asChild>
 <TooltipIconButton tooltip="Next">
 <ChevronRightIcon />
 </TooltipIconButton>
 </BranchPickerPrimitive.Next>
 </BranchPickerPrimitive.Root>
 );
};
