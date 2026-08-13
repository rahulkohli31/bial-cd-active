CREATE TABLE "app_users" (
	"entra_object_id" text PRIMARY KEY NOT NULL,
	"email" text NOT NULL,
	"display_name" text,
	"is_approver" boolean DEFAULT false NOT NULL,
	"first_seen_at" timestamp with time zone DEFAULT now() NOT NULL,
	"last_seen_at" timestamp with time zone DEFAULT now() NOT NULL
);
