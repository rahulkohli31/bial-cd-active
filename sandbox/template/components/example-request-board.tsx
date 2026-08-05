"use client";

/**
 * components/example-request-board.tsx — a worked example of a table + form + toolbar that
 * stays usable at a 390px phone width (issue #45). NOT MOUNTED ANYWHERE — deliberately a plain
 * component, not a page, so it can never become a live route in a shipped app the way an
 * app/**\/page.tsx reference would. Copy the patterns below into your own route, then delete
 * this file — it is a reference, not your feature.
 *
 * Three patterns, each called out where it happens:
 *   1. Toolbar — a row of controls that STACKS instead of overflowing under ~640px (Tailwind's
 *      `sm:` breakpoint), via `flex-col sm:flex-row`.
 *   2. Table — wide content SCROLLS INSIDE ITS OWN BOX instead of pushing the page sideways.
 *      components/ui/table.tsx already wraps every table in `overflow-x-auto`; app/globals.css
 *      backs that up for any raw table that skips the component.
 *   3. Form — fields STACK to one column on phone and pair up from `sm:` up, via
 *      `grid sm:grid-cols-2`. Dialog's own footer already stacks its buttons the same way.
 *
 * No seeded rows: per the platform's data-integrity rule, this starts empty and the form is the
 * only way rows appear — resist the urge to pre-fill it "to show what it looks like".
 */

import { zodResolver } from "@hookform/resolvers/zod";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

const STATUSES = ["open", "in_progress", "done"] as const;
type Status = (typeof STATUSES)[number];

const STATUS_LABEL: Record<Status, string> = {
  open: "Open",
  in_progress: "In progress",
  done: "Done",
};

type Request = {
  id: string;
  title: string;
  detail: string;
  status: Status;
  requestedAt: Date;
};

const requestSchema = z.object({
  title: z.string().trim().min(1, "Title is required").max(120),
  detail: z.string().trim().max(2000).optional(),
  status: z.enum(STATUSES),
});
type RequestFormValues = z.infer<typeof requestSchema>;

export function ExampleRequestBoard() {
  const [requests, setRequests] = useState<Request[]>([]);
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<Status | "all">("all");
  const [open, setOpen] = useState(false);

  const form = useForm<RequestFormValues>({
    resolver: zodResolver(requestSchema),
    defaultValues: { title: "", detail: "", status: "open" },
  });

  function onSubmit(values: RequestFormValues) {
    setRequests((prev) => [
      { id: crypto.randomUUID(), detail: "", ...values, requestedAt: new Date() },
      ...prev,
    ]);
    toast.success("Request added");
    form.reset({ title: "", detail: "", status: "open" });
    setOpen(false);
  }

  const visible = requests.filter((request) => {
    const matchesStatus = statusFilter === "all" || request.status === statusFilter;
    const matchesSearch = request.title.toLowerCase().includes(search.trim().toLowerCase());
    return matchesStatus && matchesSearch;
  });

  return (
    <main className="mx-auto flex min-h-screen max-w-4xl flex-col gap-6 px-6 py-16">
      <div className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Requests</h1>
        <p className="text-muted-foreground">
          A reference component — table, form, and toolbar that all stay usable at a 390px phone
          width.
        </p>
      </div>

      {/* PATTERN 1 — toolbar: column on phone, row from sm: up. The two filter controls are
          grouped in their own wrapping flex row so they never force the "New request" button
          off screen on a narrow phone. */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap gap-2">
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search requests..."
            className="w-full sm:w-56"
            aria-label="Search requests"
          />
          <Select
            value={statusFilter}
            onValueChange={(value) => setStatusFilter(value as Status | "all")}
          >
            <SelectTrigger className="w-full sm:w-40" aria-label="Filter by status">
              <SelectValue placeholder="All statuses" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All statuses</SelectItem>
              {STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {STATUS_LABEL[status]}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>

        <Dialog open={open} onOpenChange={setOpen}>
          <DialogTrigger asChild>
            <Button>New request</Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>New request</DialogTitle>
              <DialogDescription>
                Fields stack on a phone and pair up on wider screens.
              </DialogDescription>
            </DialogHeader>

            {/* PATTERN 3 — form: one column by default, two from sm: up. The textarea spans
                both columns at every width since it doesn't benefit from sitting side-by-side. */}
            <Form {...form}>
              <form onSubmit={form.handleSubmit(onSubmit)} className="grid gap-4 sm:grid-cols-2">
                <FormField
                  control={form.control}
                  name="title"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Title</FormLabel>
                      <FormControl>
                        <Input placeholder="What do you need?" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="status"
                  render={({ field }) => (
                    <FormItem>
                      <FormLabel>Status</FormLabel>
                      <Select value={field.value} onValueChange={field.onChange}>
                        <FormControl>
                          <SelectTrigger className="w-full">
                            <SelectValue />
                          </SelectTrigger>
                        </FormControl>
                        <SelectContent>
                          {STATUSES.map((status) => (
                            <SelectItem key={status} value={status}>
                              {STATUS_LABEL[status]}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                      <FormMessage />
                    </FormItem>
                  )}
                />
                <FormField
                  control={form.control}
                  name="detail"
                  render={({ field }) => (
                    <FormItem className="sm:col-span-2">
                      <FormLabel>Detail</FormLabel>
                      <FormControl>
                        <Textarea placeholder="Optional context" {...field} />
                      </FormControl>
                      <FormMessage />
                    </FormItem>
                  )}
                />

                <DialogFooter className="sm:col-span-2">
                  <Button type="submit">Add request</Button>
                </DialogFooter>
              </form>
            </Form>
          </DialogContent>
        </Dialog>
      </div>

      {/* PATTERN 2 — table: components/ui/table.tsx already wraps this in a `overflow-x-auto`
          box, so a wide table scrolls horizontally IN PLACE instead of widening the page. */}
      {visible.length === 0 ? (
        <p className="text-muted-foreground rounded-md border border-dashed p-8 text-center text-sm">
          {requests.length === 0
            ? "No requests yet — use “New request” to add one."
            : "No requests match this search and filter."}
        </p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Title</TableHead>
              <TableHead>Detail</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Requested</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {visible.map((request) => (
              <TableRow key={request.id}>
                <TableCell className="font-medium">{request.title}</TableCell>
                <TableCell className="text-muted-foreground max-w-xs truncate">
                  {request.detail || "—"}
                </TableCell>
                <TableCell>{STATUS_LABEL[request.status]}</TableCell>
                <TableCell>{request.requestedAt.toLocaleDateString()}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
    </main>
  );
}
