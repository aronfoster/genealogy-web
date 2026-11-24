import { initializeApp } from "firebase/app";
import {
  initializeAuth,
  inMemoryPersistence,
  browserPopupRedirectResolver,
  GoogleAuthProvider,
  signInWithPopup,
  sendSignInLinkToEmail,
  isSignInWithEmailLink,
  signInWithEmailLink,
  signOut,
} from "firebase/auth";

const result = document.querySelector("#access-result");
const panel = document.querySelector("[data-access-state]");
const completionLink = window.location.href;
const returnTo = panel?.dataset.returnTo || null;
const googleButton = document.querySelector("#google-signin");
const emailButton = document.querySelector("#signin button");
let configuration;
let emailClient;
let googleClient;
let signingIn = false;
let navigating = false;
// One-time codes stay only in this document's memory, never in history or storage.
if (panel?.hasAttribute("data-completion"))
  history.replaceState(null, "", "/access/complete");
async function configure() {
  const response = await fetch("/api/access/bootstrap", {
    credentials: "same-origin",
    cache: "no-store",
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error("unavailable");
  return response.json();
}
const show = (message) => {
  result.textContent = message;
};
function clientConfiguration() {
  configuration ??= configure().catch((error) => {
    configuration = undefined;
    throw error;
  });
  return configuration;
}
async function prepareClient(name, popup = false) {
  const config = await clientConfiguration();
  const auth = initializeAuth(initializeApp(config.firebase, name), {
    persistence: inMemoryPersistence,
    // Google can warm its helper on mobile/Safari without delaying email links.
    ...(popup ? { popupRedirectResolver: browserPopupRedirectResolver } : {}),
  });
  if (popup) {
    let timeout;
    try {
      await Promise.race([
        auth.authStateReady(),
        new Promise((_, reject) => {
          // A silently dropped helper request must leave email fallback usable.
          timeout = setTimeout(() => reject(new Error("unavailable")), 15000);
        }),
      ]);
    } finally {
      clearTimeout(timeout);
    }
  } else {
    await auth.authStateReady();
  }
  return { auth, config };
}
function prepareEmailClient() {
  emailClient ??= prepareClient("email").catch((error) => {
    emailClient = undefined;
    throw error;
  });
  return emailClient;
}
function setSigningIn(value) {
  signingIn = value;
  if (emailButton) emailButton.disabled = value;
  if (googleButton) googleButton.disabled = value || !googleClient;
}
async function finishSignIn(auth, credential) {
  const response = await post("exchange", {
    idToken: await credential.user.getIdToken(true),
    returnTo,
  });
  await signOut(auth);
  const emailInput = document.querySelector("#email");
  if (emailInput) emailInput.value = "";
  navigating = true;
  window.location.replace(response.next);
}
if (googleButton) {
  // Resolve bootstrap/SDK initialization before the click, so application-side
  // network waits cannot consume the popup's user activation.
  prepareClient("google", true)
    .then((client) => {
      googleClient = client;
      setSigningIn(signingIn);
    })
    .catch(() => {
      show(
        "Google sign-in is unavailable. Try an email link or reload this page.",
      );
    });
  googleButton.addEventListener("click", async () => {
    if (signingIn || !googleClient) return;
    const { auth } = googleClient;
    setSigningIn(true);
    show("Opening Google sign-in…");
    try {
      const provider = new GoogleAuthProvider();
      provider.setCustomParameters({ prompt: "select_account" });
      // Invoke directly from the gesture. Never retain/link collision credentials
      // or switch to redirect, which needs a different hosting/storage contract.
      const credential = await signInWithPopup(auth, provider);
      await finishSignIn(auth, credential);
    } catch (error) {
      if (error.message === "limited") {
        show(
          "Too many sign-in attempts. Please wait a few minutes and try again.",
        );
      } else if (error.code === "auth/popup-closed-by-user") {
        show("Google sign-in was cancelled. Try again or use an email link.");
      } else if (error.code === "auth/popup-blocked") {
        show("Allow the Google sign-in popup, or use an email link below.");
      } else {
        // No provider messages, email addresses, tokens or account-existence hints.
        show(
          "Google sign-in could not finish. Use an email link below, or ask the archive owner for help.",
        );
      }
    } finally {
      await signOut(auth).catch(() => {});
      if (!navigating) setSigningIn(false);
    }
  });
}
async function post(action, data) {
  const config = await configure();
  const response = await fetch(`/api/access/${action}`, {
    method: "POST",
    referrerPolicy: "same-origin",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": config.csrf,
    },
    body: JSON.stringify(data),
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok)
    throw new Error(response.status === 429 ? "limited" : "unavailable");
  return response.json();
}
function signalLogout() {
  if ("BroadcastChannel" in window) {
    const channel = new BroadcastChannel("family-access");
    channel.postMessage("changed");
    channel.close();
  } else {
    try {
      localStorage.setItem("family-access-signal", String(Date.now()));
      localStorage.removeItem("family-access-signal");
    } catch {
      /* Visibility and periodic rechecks still apply. */
    }
  }
}
document.querySelector("#signin")?.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (signingIn) return;
  const emailInput = document.querySelector("#email");
  const email = emailInput.value.trim();
  if (!/^[\x00-\x7F]+$/.test(email)) {
    show(
      "Please use an ASCII email address, including a punycode domain if needed.",
    );
    return;
  }
  setSigningIn(true);
  let auth;
  try {
    const prepared = await prepareEmailClient();
    const { config } = prepared;
    auth = prepared.auth;
    if (panel.hasAttribute("data-completion")) {
      if (!isSignInWithEmailLink(auth, completionLink))
        throw new Error("invalid-link");
      const credential = await signInWithEmailLink(auth, email, completionLink);
      await finishSignIn(auth, credential);
    } else {
      try {
        await sendSignInLinkToEmail(auth, email, {
          url: `${config.origin}/access/complete${returnTo ? `?next=${encodeURIComponent(returnTo)}` : ""}`,
          handleCodeInApp: true,
        });
      } catch {
        /* Neutral delivery copy never reveals provider account existence. */
      }
      emailInput.value = "";
      show(
        "If a sign-in link can be sent, check your inbox and spam folder. If it does not arrive, wait a few minutes before trying again.",
      );
    }
  } catch {
    show(
      panel.hasAttribute("data-completion")
        ? "This link could not complete sign-in. It may have expired or already been used. Request a new link from Family access."
        : "Sign-in is temporarily unavailable. Please try again shortly.",
    );
  } finally {
    if (auth) await signOut(auth).catch(() => {});
    if (!navigating) setSigningIn(false);
  }
});
document
  .querySelector("#access-request")
  ?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    try {
      await post("request", {
        explanation: document.querySelector("#explanation").value,
      });
      document.querySelector("#explanation").value = "";
      show(
        "Your request is waiting for review. Sending it again will not speed up review.",
      );
    } catch {
      show("Your request could not be submitted. Please wait and try again.");
    } finally {
      button.disabled = false;
    }
  });
document.querySelector("#logout")?.addEventListener("click", async () => {
  try {
    await post("logout", {});
    signalLogout();
    window.location.replace("/access");
  } catch {
    show("Sign-out could not complete. Please try again.");
  }
});
// Account navigation leads to a separate, explicit sign-out action.
function focusLogout() {
  if (window.location.hash === "#logout")
    document.querySelector("#logout")?.focus();
}
focusLogout();
window.addEventListener("hashchange", focusLogout);
