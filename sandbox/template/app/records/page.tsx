"use client";

/**
 * An EXAMPLE data screen — a real list / create / edit / delete UI wired through lib/bial-data.ts
 * against the platform data-service `items` collection. It is a starting reference, NOT a fixed
 * template: edit it, extend it, or delete it, and edit lib/bial-data.ts too (it is an editable
 * client, no longer frozen). When the data-service is unreachable (e.g. the walking skeleton
 * renders with no backend), bialData reads resolve to an empty list and the screen shows its empty
 * state — so it renders standalone.
 */

import { useCallback, useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { bialData, type RecordOut } from "@/lib/bial-data";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

const COLLECTION = "items";

function fieldStr(rec: RecordOut, key: string): string {
  const v = rec.data[key];
  return v == null ? "" : String(v);
}

export default function RecordsPage() {
  const [records, setRecords] = useState<RecordOut[]>([]);
  const [loading, setLoading] = useState(true);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<RecordOut | null>(null);
  const [title, setTitle] = useState("");
  const [note, setNote] = useState("");
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setRecords(await bialData.list(COLLECTION, { limit: 100 }));
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not load records.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openCreate() {
    setEditing(null);
    setTitle("");
    setNote("");
    setDialogOpen(true);
  }

  function openEdit(rec: RecordOut) {
    setEditing(rec);
    setTitle(fieldStr(rec, "title"));
    setNote(fieldStr(rec, "note"));
    setDialogOpen(true);
  }

  async function submit() {
    if (!title.trim()) {
      toast.error("Title is required.");
      return;
    }
    setSaving(true);
    try {
      const data = { title: title.trim(), note: note.trim() };
      if (editing) {
        await bialData.update(COLLECTION, editing.id, data);
        toast.success("Record updated.");
      } else {
        await bialData.save(COLLECTION, data);
        toast.success("Record created.");
      }
      setDialogOpen(false);
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not save the record.");
    } finally {
      setSaving(false);
    }
  }

  async function remove(rec: RecordOut) {
    try {
      await bialData.remove(COLLECTION, rec.id);
      toast.success("Record deleted.");
      await load();
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "Could not delete the record.");
    }
  }

  return (
    <main className="mx-auto max-w-4xl px-6 py-12">
      <Card>
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div className="space-y-1.5">
            <CardTitle>Records</CardTitle>
            <CardDescription>
              Example CRUD against the <code className="font-mono">{COLLECTION}</code> collection.
            </CardDescription>
          </div>
          <Button onClick={openCreate}>
            <Plus className="size-4" />
            New record
          </Button>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : records.length === 0 ? (
            <div className="text-muted-foreground rounded-md border border-dashed py-16 text-center text-sm">
              No records yet. Create your first one.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Title</TableHead>
                  <TableHead>Note</TableHead>
                  <TableHead className="w-[1%] text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {records.map((rec) => (
                  <TableRow key={rec.id}>
                    <TableCell className="font-medium">{fieldStr(rec, "title")}</TableCell>
                    <TableCell className="text-muted-foreground">{fieldStr(rec, "note")}</TableCell>
                    <TableCell className="text-right">
                      <div className="flex justify-end gap-1">
                        <Button variant="ghost" size="icon" onClick={() => openEdit(rec)} aria-label="Edit">
                          <Pencil className="size-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => void remove(rec)}
                          aria-label="Delete"
                        >
                          <Trash2 className="size-4" />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit record" : "New record"}</DialogTitle>
            <DialogDescription>
              {editing ? "Update this record's fields." : "Add a new record to the collection."}
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-2">
            <div className="grid gap-2">
              <Label htmlFor="title">Title</Label>
              <Input
                id="title"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
                placeholder="e.g. Runway inspection"
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="note">Note</Label>
              <Textarea
                id="note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="Optional details"
                rows={4}
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)} disabled={saving}>
              Cancel
            </Button>
            <Button onClick={() => void submit()} disabled={saving}>
              {saving ? "Saving…" : editing ? "Save changes" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  );
}
