//! Discord Audio & Video End-to-End Encryption (DAVE) session handling.
//!
//! This is a state machine layered on top of the `davey` crate (an MLS implementation of the
//! DAVE protocol) and the DAVE payload types from the voice gateway model crate.
//!
//! The logic deliberately mirrors `discord/voice_state.py` and
//! `discord/gateway.py` from `discord.py`, including the pending transition bookkeeping and
//! the passthrough windows used while the group is being re-keyed.

use std::collections::HashMap;
use std::num::NonZeroU16;

use davey::{DAVE_PROTOCOL_VERSION, DaveSession, ProposalsOperationType};
use voice_model as vm;
use voice_model::payload as vp;

/// An outgoing voice gateway frame produced while handling a DAVE event.
#[derive(Debug)]
pub enum Outgoing {
    /// A JSON (text) frame.
    Json(serde_json::Value),
    /// A binary frame, already opcode-prefixed.
    Binary(Vec<u8>),
}

#[derive(Debug)]
pub struct DaveState {
    session: Option<DaveSession>,
    protocol_version: u16,
    pending_transitions: HashMap<u16, u16>,
    downgraded: bool,
    user_id: u64,
    channel_id: u64,
    /// Records produced while the connection state was locked.
    ///
    /// Logging hands the message to Python, which needs the GIL. A Python thread that holds the
    /// GIL may be waiting for the very lock the caller holds (the audio path does exactly that),
    /// so emitting a record here would deadlock. The caller drains these once it has released
    /// the lock, through `with_state` in `transport`.
    pending_logs: Vec<(&'static str, String)>,
}

impl DaveState {
    pub fn new(user_id: u64, channel_id: u64) -> Self {
        Self {
            pending_logs: Vec::new(),
            session: None,
            protocol_version: 0,
            pending_transitions: HashMap::new(),
            downgraded: false,
            user_id,
            channel_id,
        }
    }

    /// The highest DAVE protocol version this library implements, advertised in IDENTIFY.
    pub const fn max_protocol_version() -> u16 {
        DAVE_PROTOCOL_VERSION
    }

    pub fn protocol_version(&self) -> u16 {
        self.protocol_version
    }

    pub fn set_protocol_version(&mut self, version: u16) {
        self.protocol_version = version;
    }

    fn note(&mut self, level: &'static str, message: String) {
        self.pending_logs.push((level, message));
    }

    fn note_debug(&mut self, message: String) {
        self.note("debug", message);
    }

    fn note_warning(&mut self, message: String) {
        self.note("warning", message);
    }

    /// Take the records produced since the last call. The caller has to have released the
    /// connection lock by now.
    pub fn take_logs(&mut self) -> Vec<(&'static str, String)> {
        std::mem::take(&mut self.pending_logs)
    }

    pub fn set_channel_id(&mut self, channel_id: u64) {
        self.channel_id = channel_id;
    }

    pub fn has_session(&self) -> bool {
        self.session.is_some()
    }

    pub fn is_ready(&self) -> bool {
        self.session.as_ref().is_some_and(DaveSession::is_ready)
    }

    /// `discord.py`'s `can_encrypt`: a session exists, is ready, and we are not on version 0.
    pub fn can_encrypt(&self) -> bool {
        self.protocol_version != 0 && self.is_ready()
    }

    pub fn voice_privacy_code(&self) -> Option<String> {
        self.session
            .as_ref()
            .and_then(DaveSession::voice_privacy_code)
            .map(str::to_owned)
    }

    fn to_dave_err<E: std::fmt::Display>(err: E) -> crate::errors::DaveFailure {
        crate::errors::DaveFailure::Davey(err.to_string())
    }

    /// Encrypt an Opus frame for transmission, if the session is able to.
    pub fn encrypt_opus(
        &mut self,
        data: &[u8],
    ) -> Result<Option<Vec<u8>>, crate::errors::DaveFailure> {
        if !self.can_encrypt() {
            return Ok(None);
        }
        let Some(session) = self.session.as_mut() else {
            return Ok(None);
        };
        let encrypted = session.encrypt_opus(data).map_err(Self::to_dave_err)?;
        Ok(Some(encrypted.into_owned()))
    }

    /// (Re)create the MLS session for the current protocol version and return the
    /// `dave_mls_key_package` (opcode 26) frame that must be sent to Discord.
    ///
    pub fn reinit(&mut self) -> Result<Vec<Outgoing>, crate::errors::DaveFailure> {
        let mut outgoing = Vec::new();

        if self.protocol_version > 0 {
            let version = NonZeroU16::new(self.protocol_version).ok_or_else(|| {
                crate::errors::DaveFailure::Davey("DAVE protocol version 0".to_owned())
            })?;

            match self.session.as_mut() {
                Some(session) => session
                    .reinit(version, self.user_id, self.channel_id, None)
                    .map_err(Self::to_dave_err)?,
                None => {
                    self.session = Some(
                        DaveSession::new(version, self.user_id, self.channel_id, None)
                            .map_err(Self::to_dave_err)?,
                    );
                }
            }

            let key_package = self
                .session
                .as_mut()
                .expect("session was just created")
                .create_key_package()
                .map_err(Self::to_dave_err)?;

            self.send_binary(
                &vm::Event::DaveMlsKeyPackage(vp::DaveMlsKeyPackage { key_package }),
                &mut outgoing,
            )?;
        } else if let Some(session) = self.session.as_mut() {
            let _ = session.reset();
            session.set_passthrough_mode(true, Some(10));
        }

        Ok(outgoing)
    }

    fn send_binary(
        &self,
        event: &vm::Event,
        outgoing: &mut Vec<Outgoing>,
    ) -> Result<(), crate::errors::DaveFailure> {
        let bytes = vm::serialize_binary_event(event)
            .map_err(|err| crate::errors::DaveFailure::Davey(err.to_string()))?;
        outgoing.push(Outgoing::Binary(bytes));
        Ok(())
    }

    fn transition_ready(&mut self, transition_id: u16, outgoing: &mut Vec<Outgoing>) {
        let event = vm::Event::DaveTransitionReady(vp::DaveTransitionReady {
            transition_id,
            protocol_version: self.protocol_version,
        });
        match serde_json::to_value(&event) {
            Ok(value) => outgoing.push(Outgoing::Json(value)),
            Err(err) => {
                self.note_warning(format!("unable to serialize DAVE transition ready: {err}"))
            }
        }
    }

    /// `dave_prepare_transition` (opcode 21).
    pub fn handle_prepare_transition(
        &mut self,
        transition_id: u16,
        protocol_version: u16,
    ) -> Result<Vec<Outgoing>, crate::errors::DaveFailure> {
        let mut outgoing = Vec::new();
        self.pending_transitions
            .insert(transition_id, protocol_version);

        if transition_id == 0 {
            self.execute_transition(transition_id);
        } else {
            if protocol_version == 0
                && let Some(session) = self.session.as_mut()
            {
                session.set_passthrough_mode(true, Some(120));
            }
            self.transition_ready(transition_id, &mut outgoing);
        }

        Ok(outgoing)
    }

    /// `dave_execute_transition` (opcode 22).
    pub fn handle_execute_transition(&mut self, transition_id: u16) -> Vec<Outgoing> {
        self.execute_transition(transition_id);
        Vec::new()
    }

    /// `dave_prepare_epoch` (opcode 24).
    pub fn handle_prepare_epoch(
        &mut self,
        epoch: u64,
        protocol_version: u16,
    ) -> Result<Vec<Outgoing>, crate::errors::DaveFailure> {
        // When the epoch is 1 a brand new MLS group is created for the given protocol version.
        if epoch == 1 {
            self.protocol_version = protocol_version;
            return self.reinit();
        }
        Ok(Vec::new())
    }

    /// Mirrors `VoiceConnectionState._execute_transition`.
    fn execute_transition(&mut self, transition_id: u16) {
        let before = (self.protocol_version, self.can_encrypt());
        let Some(new_version) = self.pending_transitions.remove(&transition_id) else {
            self.note_warning(format!(
                "received execute transition, but no pending transition for id {transition_id}"
            ));
            return;
        };

        let old_version = self.protocol_version;
        self.protocol_version = new_version;

        if old_version != new_version && new_version == 0 {
            self.downgraded = true;
            self.note_debug("DAVE session downgraded".to_owned());
        } else if transition_id > 0 && self.downgraded {
            self.downgraded = false;
            if let Some(session) = self.session.as_mut() {
                session.set_passthrough_mode(true, Some(10));
            }
            self.note_debug("DAVE session upgraded".to_owned());
        }

        let (version_before, encrypted_before) = before;
        let (version_after, encrypted_after) = (self.protocol_version, self.can_encrypt());
        self.note_debug(format!(
            "DAVE transition {transition_id} executed: protocol_version {version_before} -> \
             {version_after}, can_encrypt {encrypted_before} -> {encrypted_after}"
        ));
    }

    /// Build the `dave_mls_invalid_commit_welcome` (opcode 31) recovery frame and re-create the
    /// session, as `VoiceConnectionState._recover_from_invalid_commit` does.
    pub fn recover_from_invalid_commit(
        &mut self,
        transition_id: u16,
    ) -> Result<Vec<Outgoing>, crate::errors::DaveFailure> {
        let mut outgoing = vec![Outgoing::Json(serde_json::json!({
            "op": vm::Opcode::DaveMlsInvalidCommitWelcome as u8,
            "d": { "transition_id": transition_id },
        }))];
        outgoing.extend(self.reinit()?);
        Ok(outgoing)
    }

    /// Handle a binary DAVE frame. `payload` must already have the 2 byte voice gateway
    /// sequence number stripped (i.e. it starts with the opcode).
    pub fn handle_binary(
        &mut self,
        payload: &[u8],
    ) -> Result<Vec<Outgoing>, crate::errors::DaveFailure> {
        let event = match vm::deserialize_binary_event(payload) {
            Ok(event) => event,
            Err(err) => {
                self.note_debug(format!("ignoring unknown DAVE binary frame: {err}"));
                return Ok(Vec::new());
            }
        };

        let mut outgoing = Vec::new();
        if self.session.is_none() {
            return Ok(outgoing);
        }

        match event {
            vm::Event::DaveMlsExternalSender(data) => {
                self.session
                    .as_mut()
                    .expect("checked above")
                    .set_external_sender(&data.external_sender)
                    .map_err(Self::to_dave_err)?;
                self.note_debug("set MLS external sender".to_owned());
            }
            vm::Event::DaveMlsProposals(data) => {
                let op = match data.operation_type {
                    vp::DaveMlsProposalsOperationType::Append => ProposalsOperationType::APPEND,
                    vp::DaveMlsProposalsOperationType::Revoke => ProposalsOperationType::REVOKE,
                };
                let result = self
                    .session
                    .as_mut()
                    .expect("checked above")
                    .process_proposals(op, &data.proposals, None)
                    .map_err(Self::to_dave_err)?;

                if let Some(commit_welcome) = result {
                    self.send_binary(
                        &vm::Event::DaveMlsCommitWelcome(vp::DaveMlsCommitWelcome {
                            commit: commit_welcome.commit,
                            welcome: commit_welcome.welcome,
                        }),
                        &mut outgoing,
                    )?;
                }
                self.note_debug("MLS proposals processed".to_owned());
            }
            vm::Event::DaveMlsAnnounceCommitTransition(data) => {
                let transition_id = data.transition_id;
                let processed = self
                    .session
                    .as_mut()
                    .expect("checked above")
                    .process_commit(&data.commit_message);

                if let Err(err) = processed {
                    self.note_warning(format!("invalid MLS commit: {err}"));
                    outgoing.extend(self.recover_from_invalid_commit(transition_id)?);
                } else {
                    if transition_id != 0 {
                        self.pending_transitions
                            .insert(transition_id, self.protocol_version);
                        self.transition_ready(transition_id, &mut outgoing);
                    }
                    self.note_debug(format!(
                        "MLS commit processed for transition {transition_id}"
                    ));
                }
            }
            vm::Event::DaveMlsWelcome(data) => {
                let transition_id = data.transition_id;
                let processed = self
                    .session
                    .as_mut()
                    .expect("checked above")
                    .process_welcome(&data.welcome);

                if let Err(err) = processed {
                    self.note_warning(format!("invalid MLS welcome: {err}"));
                    outgoing.extend(self.recover_from_invalid_commit(transition_id)?);
                } else {
                    if transition_id != 0 {
                        self.pending_transitions
                            .insert(transition_id, self.protocol_version);
                        self.transition_ready(transition_id, &mut outgoing);
                    }
                    self.note_debug(format!(
                        "MLS welcome processed for transition {transition_id}"
                    ));
                }
            }
            _ => {}
        }

        Ok(outgoing)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn logs_are_buffered_until_the_caller_takes_them() {
        // The caller holds the connection lock while this runs, so nothing may be emitted yet:
        // handing a record to Python needs the GIL, which the audio thread may be holding while
        // it waits for that very lock.
        let mut dave = DaveState::new(1, 2);
        let outgoing = dave.handle_binary(&[99, 1, 2, 3]).unwrap();
        assert!(outgoing.is_empty());

        let logs = dave.take_logs();
        assert_eq!(logs.len(), 1, "the record has to wait for the caller");
        assert_eq!(logs[0].0, "debug");
        assert!(
            logs[0].1.contains("unknown DAVE binary frame"),
            "{:?}",
            logs[0]
        );

        // Taking them drains the buffer.
        assert!(dave.take_logs().is_empty());
    }

    #[test]
    fn pending_transition_downgrade_and_upgrade() {
        let mut dave = DaveState::new(1, 2);
        dave.protocol_version = 1;

        // Transition to 0 downgrades the session.
        dave.pending_transitions.insert(4, 0);
        dave.execute_transition(4);
        assert_eq!(dave.protocol_version, 0);
        assert!(dave.downgraded);

        // A later transition > 0 clears the downgrade flag.
        dave.pending_transitions.insert(5, 1);
        dave.execute_transition(5);
        assert_eq!(dave.protocol_version, 1);
        assert!(!dave.downgraded);
    }

    #[test]
    fn unknown_transition_is_ignored() {
        let mut dave = DaveState::new(1, 2);
        dave.protocol_version = 1;
        dave.execute_transition(99);
        assert_eq!(dave.protocol_version, 1);
    }

    #[test]
    fn max_protocol_version_matches_davey() {
        assert_eq!(DaveState::max_protocol_version(), 1);
    }

    #[test]
    fn key_package_frame_is_binary_opcode_26() {
        let mut dave = DaveState::new(42, 7);
        dave.protocol_version = 1;
        let outgoing = dave.reinit().unwrap();
        match &outgoing[0] {
            Outgoing::Binary(bytes) => assert_eq!(bytes[0], 26),
            other => panic!("expected a binary frame, got {other:?}"),
        }
    }
}
