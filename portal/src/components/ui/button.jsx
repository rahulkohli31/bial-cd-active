import * as React from"react"
import { Slot } from"@radix-ui/react-slot"
import { cva } from"class-variance-authority";

import { cn } from"@/lib/utils"

const buttonVariants = cva(
"inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-md text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-bial-border disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
 {
 variants: {
 variant: {
 default:
"bg-tertiary text-white shadow hover:bg-tertiary",
 destructive:
"bg-danger text-white shadow-sm hover:bg-danger",
 outline:
"border border-surface-muted bg-white shadow-sm hover:bg-white hover:text-tertiary",
 secondary:
"bg-white text-tertiary shadow-sm hover:bg-white",
 ghost:"hover:bg-white hover:text-tertiary",
 link:"text-tertiary underline-offset-4 hover:underline",
 },
 size: {
 default:"h-9 px-4 py-2",
 sm:"h-8 rounded-md px-3 text-xs",
 lg:"h-10 rounded-md px-8",
 icon:"h-9 w-9",
 },
 },
 defaultVariants: {
 variant:"default",
 size:"default",
 },
 }
)

const Button = React.forwardRef(({ className, variant, size, asChild = false, ...props }, ref) => {
 const Comp = asChild ? Slot :"button"
 return (
 <Comp
 className={cn(buttonVariants({ variant, size, className }))}
 ref={ref}
 {...props} />
 );
})
Button.displayName ="Button"

export { Button, buttonVariants }
