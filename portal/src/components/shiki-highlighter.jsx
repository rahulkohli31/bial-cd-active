"use client";;
import { useShikiHighlighter } from"react-shiki";
import { useAui, useAuiState } from"@assistant-ui/react";
import { cn } from"@/lib/utils";

const containerClassName =
"aui-shiki-base [&_pre]:border-surface-muted [&_pre]:bg-white! [&_.line]:px-0! [&_pre]:overflow-x-auto [&_pre]:rounded-t-none [&_pre]:rounded-b-xl [&_pre]:border [&_pre]:border-t-0 [&_pre]:p-3.5 [&_pre]:text-[13px] [&_pre]:leading-relaxed dark:[&_pre]:border-white dark:[&_pre]:bg-tertiary!";

const PlainCode = ({ code }) => (
 <pre>
 <code>{code}</code>
 </pre>
);

const HighlightedCode = ({ code, language, theme, options }) => {
 const highlighted = useShikiHighlighter(code, language, theme, {
 ...options,
 defaultColor:"light-dark()",
 });
 return <>{highlighted ?? <PlainCode code={code} />}</>;
};

/**
 * SyntaxHighlighter component, using react-shiki
 * Use it by passing to `defaultComponents` in `markdown-text.tsx`
 *
 * Skips tokenization while the message part is streaming and renders the
 * plain code in the same container, so streaming costs no Shiki work and
 * settling is a color change rather than a layout shift.
 *
 * @example
 * const defaultComponents = memoizeMarkdownComponents({
 * SyntaxHighlighter,
 * h1: //...
 * //...other elements...
 * });
 */
export const SyntaxHighlighter = ({
 code,
 language,
 theme = { dark:"github-dark-default", light:"github-light-default"},
 className,
 style,
 // Inert: useShikiHighlighter output has no default styles or language label.
 addDefaultStyles: _addDefaultStyles,
 showLanguage: _showLanguage,
 delay = 150, // the part settles before smooth streaming finishes draining, so code keeps changing for a few frames
 node: _node,
 components: _components,
 ...options
}) => {
 const aui = useAui();
 const hasPart = aui.part.source !== null;
 const isStreaming = useAuiState((s) => hasPart && s.part.status.type ==="running");
 const trimmed = code.trim();

 return (
 <div
 className={cn(containerClassName, isStreaming &&"aui-shiki-streaming", className)}
 style={style}>
 {isStreaming ? (
 <PlainCode code={trimmed} />
 ) : (
 <HighlightedCode
 code={trimmed}
 language={language}
 theme={theme}
 options={{ ...options, delay }} />
 )}
 </div>
 );
};

SyntaxHighlighter.displayName ="SyntaxHighlighter";
