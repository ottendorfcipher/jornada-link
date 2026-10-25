"""The JavaScript-for-Automation sources that drive Mail.app. Each ``run(argv)`` takes
plain string arguments and returns JSON, so nothing is ever interpolated into script text."""
from __future__ import annotations

LOCATE = """
function locate(Mail, accountName, mailboxName) {
  if (accountName) {
    return [Mail.accounts.byName(accountName).mailboxes.byName(mailboxName)];
  }
  if (mailboxName.toUpperCase() === "INBOX") {
    return [Mail.inbox];
  }
  const found = [];
  Mail.accounts().forEach(function (account) {
    account.mailboxes().forEach(function (box) {
      if (box.name() === mailboxName) { found.push(box); }
    });
  });
  return found;
}
function findMessage(boxes, id) {
  for (let i = 0; i < boxes.length; i++) {
    try {
      const message = boxes[i].messages.byId(id);
      message.id();
      return message;
    } catch (error) { /* not in this mailbox */ }
  }
  return null;
}
"""

CONNECT = LOCATE + """
function run(argv) {
  const Mail = Application("Mail");
  const boxes = locate(Mail, argv[0], argv[1]);
  let count = 0;
  boxes.forEach(function (box) { box.name(); count += 1; });
  return JSON.stringify({mailboxes: count});
}
"""

LIST = LOCATE + """
function recipients(list) {
  const names = list.name();
  return list.address().map(function (address, i) { return {name: names[i] || "", address: address || ""}; });
}
function details(box, id) {
  const m = box.messages.byId(id);
  const item = {id: id, error: null};
  try {
    item.subject = m.subject();
    item.sender = m.sender();
    item.to = recipients(m.toRecipients);
    item.cc = recipients(m.ccRecipients);
    item.date = m.dateSent();
    item.messageId = m.messageId();
    item.content = m.content();
  } catch (error) {
    item.error = String(error);
  }
  return item;
}
function run(argv) {
  const Mail = Application("Mail");
  const limit = parseInt(argv[2], 10);
  const boxes = locate(Mail, argv[0], argv[1]);
  let entries = [];
  boxes.forEach(function (box) {
    const ids = box.messages.id();
    const dates = box.messages.dateReceived();
    ids.forEach(function (id, i) { entries.push({box: box, id: id, date: dates[i] ? dates[i].getTime() : 0}); });
  });
  entries.sort(function (a, b) { return b.date - a.date; });
  entries = entries.slice(0, limit).reverse();
  return JSON.stringify(entries.map(function (entry) { return details(entry.box, entry.id); }));
}
"""

DELETE = LOCATE + """
function run(argv) {
  const Mail = Application("Mail");
  const mode = argv[2];
  const archiveName = argv[3];
  const ids = JSON.parse(argv[4]);
  const boxes = locate(Mail, argv[0], argv[1]);
  const errors = [];
  let done = 0;
  ids.forEach(function (id) {
    const message = findMessage(boxes, id);
    if (message === null) { errors.push("message " + id + " not found"); return; }
    try {
      if (mode === "delete") {
        message.deletedStatus = true;
      } else if (mode === "archive") {
        const target = message.mailbox().account().mailboxes.byName(archiveName);
        Mail.move(message, {to: target});
      } else {
        message.readStatus = true;
      }
      done += 1;
    } catch (error) {
      errors.push("message " + id + ": " + String(error));
    }
  });
  return JSON.stringify({done: done, errors: errors});
}
"""

SEND = """
function run(argv) {
  const Mail = Application("Mail");
  const message = Mail.OutgoingMessage({subject: argv[0], content: argv[1], visible: false});
  Mail.outgoingMessages.push(message);
  if (argv[2]) { message.sender = argv[2]; }
  JSON.parse(argv[3]).forEach(function (address) {
    message.toRecipients.push(Mail.Recipient({address: address}));
  });
  const sent = message.send();
  return JSON.stringify({sent: sent !== false});
}
"""
