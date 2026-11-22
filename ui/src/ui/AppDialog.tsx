import { useId, type FormEvent, type ReactNode } from "react";
import { Dialog, DialogHeader } from "@astryxdesign/core/Dialog";
import { Layout, LayoutContent, LayoutFooter } from "@astryxdesign/core/Layout";
import { HStack } from "@astryxdesign/core/HStack";

/**
 * Every dialog in the app: Astryx's Dialog with its header (the title, a line
 * under it, the close button), a body that scrolls, and a footer that keeps
 * the actions in reach. Escape and the close button close it; the focus goes
 * back to what opened it.
 *
 * A dialog is mounted while it is open (`{open && <AppDialog … />}`). With
 * `onSubmit` the body is a form: give the footer's main button
 * `type="submit"` and `form={formId}` through the `footer` function.
 */
export function AppDialog(props: {
  title: string;
  /** One line under the title: what the dialog is for. */
  subtitle?: ReactNode;
  onClose: () => void;
  /** The dialog's width in pixels; it never outgrows the window. */
  width?: number;
  /** `form` keeps a click outside from throwing away what was typed. */
  purpose?: "info" | "form" | "required";
  /** The actions, at the end of the footer. A function gets the id of the body's form. */
  footer?: ReactNode | ((formId: string) => ReactNode);
  /** What the footer says at its start, opposite the actions. */
  footerNote?: ReactNode;
  onSubmit?: (e: FormEvent) => void;
  children: ReactNode;
}) {
  const { onClose, onSubmit } = props;
  const formId = useId();
  const footer = typeof props.footer === "function" ? props.footer(formId) : props.footer;
  return (
    <Dialog
      isOpen
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      width={props.width ?? 480}
      maxHeight="85dvh"
      purpose={props.purpose ?? (onSubmit ? "form" : "info")}
    >
      <Layout
        header={<DialogHeader title={props.title} subtitle={props.subtitle} onOpenChange={() => onClose()} />}
        content={
          <LayoutContent>
            {onSubmit ? (
              <form
                id={formId}
                onSubmit={(e) => {
                  e.preventDefault();
                  onSubmit(e);
                }}
              >
                {props.children}
              </form>
            ) : (
              props.children
            )}
          </LayoutContent>
        }
        footer={
          footer || props.footerNote ? (
            <LayoutFooter>
              <HStack gap={2} hAlign={props.footerNote ? "between" : "end"} vAlign="center">
                {props.footerNote}
                <HStack gap={2}>{footer}</HStack>
              </HStack>
            </LayoutFooter>
          ) : undefined
        }
      />
    </Dialog>
  );
}
