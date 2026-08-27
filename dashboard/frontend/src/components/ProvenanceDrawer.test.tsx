import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ProvenanceDrawer } from "./ProvenanceDrawer";
import { CLAIM_A, DELTA_A, provenance } from "../test/fixtures";

function open(overrides: Partial<Parameters<typeof ProvenanceDrawer>[0]> = {}) {
  const onClose = vi.fn();
  const onOpenClaim = vi.fn();
  const utils = render(
    <>
      <button type="button">outside trigger</button>
      <ProvenanceDrawer
        open
        loading={false}
        error={null}
        data={provenance()}
        requested={{ claimId: CLAIM_A, version: 1 }}
        onClose={onClose}
        onOpenClaim={onOpenClaim}
        {...overrides}
      />
    </>,
  );
  return { ...utils, onClose, onOpenClaim };
}

describe("provenance drawer", () => {
  it("is a modal dialog with an accessible name", () => {
    open();
    const dialog = screen.getByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog).toHaveAccessibleName("Provenance");
  });

  it("moves focus to the close button when it opens", () => {
    open();
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
  });

  it("closes on Escape", async () => {
    const { onClose } = open();
    await userEvent.keyboard("{Escape}");
    expect(onClose).toHaveBeenCalled();
  });

  it("closes when the backdrop is clicked", async () => {
    const { onClose } = open();
    await userEvent.click(screen.getByTestId("drawer-backdrop"));
    expect(onClose).toHaveBeenCalled();
  });

  it("keeps Tab focus inside the drawer", async () => {
    open();
    const dialog = screen.getByRole("dialog");
    const focusable = Array.from(dialog.querySelectorAll("button"));
    focusable[focusable.length - 1].focus();
    await userEvent.tab();
    expect(dialog.contains(document.activeElement)).toBe(true);
    await userEvent.tab({ shift: true });
    expect(dialog.contains(document.activeElement)).toBe(true);
  });

  it("shows the claim, its exact version, and the canonical Delta", () => {
    open();
    expect(screen.getAllByText(new RegExp(`${CLAIM_A} · v1`))).not.toHaveLength(0);
    expect(screen.getByText("v1 (exact)")).toBeInTheDocument();
    expect(screen.getByText(DELTA_A)).toBeInTheDocument();
    expect(screen.getByText("meaningful")).toBeInTheDocument();
  });

  it("renders the grounded quote as text, never as markup", () => {
    const data = provenance();
    data.evidence[0].changes[0].quote_after = '<b>bold</b> and <script>alert(1)</script>';
    const { container } = open({ data });
    const quote = screen.getByText(/bold/);
    expect(quote.tagName).toBe("BLOCKQUOTE");
    expect(container.querySelector("blockquote b")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("shows observation metadata and the configured source, not a raw payload", () => {
    const { container } = open();
    expect(screen.getByText(/obs_01M0YF3Q87GQZ38YYFF2W8N2AF/)).toBeInTheDocument();
    expect(screen.getByText("https://github.com/anthropics/claude-code")).toBeInTheDocument();
    expect(container.textContent).not.toContain("gs://");
  });

  it("says plainly when a claim has no supersession or dispute links", () => {
    open();
    expect(screen.getByText(/No supersession or dispute links/)).toBeInTheDocument();
  });

  it("follows a supersession link to the replacement claim", async () => {
    const data = provenance();
    data.lifecycle.superseded_by = "clm_01M0YFMB5EEFBEQX2F6EJ3YC2T";
    const { onOpenClaim } = open({ data });
    await userEvent.click(
      screen.getByRole("button", { name: "clm_01M0YFMB5EEFBEQX2F6EJ3YC2T" }),
    );
    expect(onOpenClaim).toHaveBeenCalledWith("clm_01M0YFMB5EEFBEQX2F6EJ3YC2T", 1);
  });

  it("reports a reconstructed version honestly", () => {
    const data = provenance();
    data.exact_version = false;
    data.requested_version = 1;
    data.current_version = 3;
    data.reconstruction_note = "version 1 is reconstructed from the claim's embedded history";
    open({ data });
    expect(screen.getByText("v1 (reconstructed)")).toBeInTheDocument();
    expect(screen.getByText(/reconstructed from the claim/)).toBeInTheDocument();
  });

  it("renders nothing when it is closed", () => {
    render(
      <ProvenanceDrawer
        open={false}
        loading={false}
        error={null}
        data={null}
        requested={null}
        onClose={vi.fn()}
        onOpenClaim={vi.fn()}
      />,
    );
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("shows a bounded error instead of a blank drawer", () => {
    open({ data: null, error: "That resource does not exist." });
    expect(screen.getByText("That resource does not exist.")).toBeInTheDocument();
  });
});
