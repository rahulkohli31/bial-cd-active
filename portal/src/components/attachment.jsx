"use client";;
import { useEffect, useState } from"react";
import {
 XIcon,
 PlusIcon,
 FileText,
 Loader2Icon,
 AlertCircleIcon,
} from"lucide-react";
import {
 AttachmentPrimitive,
 ComposerPrimitive,
 MessagePrimitive,
 useAuiState,
 useAui,
} from"@assistant-ui/react";
import { useShallow } from"zustand/shallow";
import {
 Tooltip,
 TooltipContent,
 TooltipTrigger,
} from"@/components/ui/tooltip";
import {
 Dialog,
 DialogTitle,
 DialogContent,
 DialogTrigger,
} from"@/components/ui/dialog";
import {
 Avatar,
 AvatarImage,
 AvatarFallback,
} from"@/components/ui/avatar";
import { TooltipIconButton } from"@/components/tooltip-icon-button";
import { cn } from"@/lib/utils";

const useFileSrc = (file) => {
 const [src, setSrc] = useState(undefined);

 useEffect(() => {
 if (!file) {
 setSrc(undefined);
 return;
 }

 const objectUrl = URL.createObjectURL(file);
 setSrc(objectUrl);

 return () => {
 URL.revokeObjectURL(objectUrl);
 };
 }, [file]);

 return src;
};

const useAttachmentSrc = () => {
 const { file, src } = useAuiState(useShallow(s => {
 if (s.attachment.type !=="image") return {};
 if (s.attachment.file) return { file: s.attachment.file };
 const src = s.attachment.content?.filter((c) => c.type ==="image")[0]
 ?.image;
 if (!src) return {};
 return { src };
 }));

 return useFileSrc(file) ?? src;
};

const AttachmentPreview = ({ src }) => {
 const [isLoaded, setIsLoaded] = useState(false);
 return (
 <img
 src={src}
 alt="Attachment preview"
 className={cn("block h-auto max-h-[80vh] w-auto max-w-full object-contain", isLoaded
 ?"aui-attachment-preview-image-loaded"
 :"aui-attachment-preview-image-loading invisible")}
 onLoad={() => setIsLoaded(true)} />
 );
};

const AttachmentPreviewDialog = ({ children }) => {
 const src = useAttachmentSrc();

 if (!src) return children;

 return (
 <Dialog>
 <DialogTrigger
 className="aui-attachment-preview-trigger hover:bg-white cursor-pointer transition-colors"
 asChild>
 {children}
 </DialogTrigger>
 <DialogContent
 className="aui-attachment-preview-dialog-content [&>button]:bg-tertiary [&_svg]:text-white [&>button]:hover:[&_svg]:text-danger p-2 sm:max-w-3xl [&>button]:rounded-full [&>button]:p-1 [&>button]:opacity-100 [&>button]:ring-0! dark:[&>button]:bg-white dark:[&_svg]:text-tertiary dark:[&>button]:hover:[&_svg]:text-danger">
 <DialogTitle className="aui-sr-only sr-only">
 Image Attachment Preview
 </DialogTitle>
 <div
 className="aui-attachment-preview bg-white relative mx-auto flex max-h-[80dvh] w-full items-center justify-center overflow-hidden">
 <AttachmentPreview src={src} />
 </div>
 </DialogContent>
 </Dialog>
 );
};

const AttachmentThumb = () => {
 const src = useAttachmentSrc();

 return (
 <Avatar className="aui-attachment-tile-avatar h-full w-full rounded-none">
 <AvatarImage
 src={src}
 alt="Attachment preview"
 className="aui-attachment-tile-image object-cover"/>
 <AvatarFallback>
 <FileText
 className="aui-attachment-tile-fallback-icon text-neutral size-8"/>
 </AvatarFallback>
 </Avatar>
 );
};

const AttachmentUI = () => {
 const aui = useAui();
 const isComposer = aui.attachment.source !=="message";

 const isImage = useAuiState((s) => s.attachment.type ==="image");
 const typeLabel = useAuiState((s) => {
 const type = s.attachment.type;
 switch (type) {
 case"image":
 return"Image";
 case"document":
 return"Document";
 case"file":
 return"File";
 default:
 return type;
 }
 });

 const uploadState = useAuiState((s) =>
 s.attachment.status.type ==="running"
 ?"uploading"
 : s.attachment.status.type ==="incomplete"&&
 s.attachment.status.reason ==="error"
 ?"error"
 : undefined);
 const isUploading = uploadState ==="uploading";
 const isError = uploadState ==="error";

 const errorMessage = useAuiState((s) =>
 s.attachment.status.type ==="incomplete"&&
 s.attachment.status.reason ==="error"
 ? (s.attachment.status.message ??"Upload failed")
 : undefined);

 return (
 <Tooltip>
 <AttachmentPrimitive.Root
 className={cn("aui-attachment-root relative", isImage &&
 !isComposer &&
"aui-attachment-root-message only:*:first:size-24")}>
 <AttachmentPreviewDialog>
 <TooltipTrigger asChild>
 <div
 className={cn(
"aui-attachment-tile bg-white relative size-14 cursor-pointer overflow-hidden rounded-[calc(var(--composer-radius)-var(--composer-padding))] border border-surface-muted transition-opacity hover:opacity-75",
 isError &&"border-danger"
 )}
 role="button"
 tabIndex={0}
 aria-label={`${typeLabel} attachment${
 isError ?", upload failed": isUploading ?", uploading":""
 }`}>
 <AttachmentThumb />
 {isUploading && (
 <div
 aria-hidden="true"
 className="aui-attachment-tile-uploading bg-white absolute inset-0 flex items-center justify-center backdrop-blur-[1px]">
 <Loader2Icon
 className="text-neutral size-5 animate-spin"/>
 </div>
 )}
 {isError && (
 <div
 aria-hidden="true"
 className="aui-attachment-tile-error bg-danger absolute inset-0 flex items-center justify-center">
 <AlertCircleIcon
 className="text-danger size-5"/>
 </div>
 )}
 </div>
 </TooltipTrigger>
 </AttachmentPreviewDialog>
 {isComposer && <AttachmentRemove />}
 </AttachmentPrimitive.Root>
 <TooltipContent side="top">
 <AttachmentPrimitive.Name />
 {errorMessage && (
 <p className="aui-attachment-error-message">{errorMessage}</p>
 )}
 </TooltipContent>
 </Tooltip>
 );
};

const AttachmentRemove = () => {
 return (
 <AttachmentPrimitive.Remove asChild>
 <TooltipIconButton
 tooltip="Remove file"
 className="aui-attachment-tile-remove text-neutral hover:[&_svg]:text-danger absolute end-1.5 top-1.5 size-3.5 rounded-full bg-white opacity-100 shadow-sm hover:bg-white! [&_svg]:text-black dark:hover:[&_svg]:text-danger"
 side="top">
 <XIcon className="aui-attachment-remove-icon size-3 dark:stroke-[2.5px]"/>
 </TooltipIconButton>
 </AttachmentPrimitive.Remove>
 );
};

export const UserMessageAttachments = () => {
 return (
 <div
 className="aui-user-message-attachments-end col-span-full col-start-1 row-start-1 flex w-full flex-row justify-end gap-2">
 <MessagePrimitive.Attachments>
 {() => <AttachmentUI />}
 </MessagePrimitive.Attachments>
 </div>
 );
};

export const ComposerAttachments = () => {
 return (
 <div
 className="aui-composer-attachments flex w-full flex-row items-center gap-2 overflow-x-auto empty:hidden">
 <ComposerPrimitive.Attachments>
 {() => <AttachmentUI />}
 </ComposerPrimitive.Attachments>
 </div>
 );
};

export const ComposerAddAttachment = () => {
 return (
 <ComposerPrimitive.AddAttachment asChild>
 <TooltipIconButton
 tooltip="Add Attachment"
 side="bottom"
 variant="ghost"
 size="icon"
 className="aui-composer-add-attachment hover:bg-neutral size-7 rounded-full p-1 text-xs font-semibold"
 aria-label="Add Attachment">
 <PlusIcon className="aui-attachment-add-icon size-4.5 stroke-[1.5px]"/>
 </TooltipIconButton>
 </ComposerPrimitive.AddAttachment>
 );
};
