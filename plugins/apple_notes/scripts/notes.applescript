on run argv
	if (count of argv) is not 8 then
		return my errorEnvelope("validate_arguments", -17000, "Apple Notes bridge requires exactly 8 arguments")
	end if

	set actionName to item 1 of argv
	set accountName to item 2 of argv
	set folderName to item 3 of argv
	set shouldCreateFolder to (item 4 of argv is "true")
	set noteTitle to item 5 of argv
	set htmlPath to item 6 of argv
	set noteIdentifier to item 7 of argv
	set markerText to item 8 of argv

	if actionName is "create" then
		if noteTitle is "" then return my errorEnvelope("validate_title", -17000, "Apple Note title is missing")
		return my createNote(accountName, folderName, shouldCreateFolder, noteTitle, htmlPath)
	else if actionName is "append" then
		return my appendNote(accountName, folderName, shouldCreateFolder, noteIdentifier, htmlPath)
	else if actionName is "find" then
		return my findMarker(accountName, folderName, shouldCreateFolder, markerText)
	end if
	return my errorEnvelope("validate_action", -17000, "Unsupported Apple Notes action")
end run


on createNote(accountName, folderName, shouldCreateFolder, noteTitle, htmlPath)
	set stageName to "resolve_account"
	try
		set htmlText to my readUtf8(htmlPath)
		tell application "Notes"
			set targetAccount to my resolveAccount(accountName)
			set stageName to "resolve_folder"
			set targetFolder to my resolveFolder(targetAccount, folderName, shouldCreateFolder)
			if shared of targetFolder then error "Target folder is shared" number -17005

			set stageName to "create_note"
			-- Notes derives the note name from the first rendered body line. Setting
			-- both name and body prepends a second plain-text title in the editor.
			set createdNote to make new note at targetFolder with properties {body:htmlText}
			set createdId to id of createdNote as text
			set targetFolderId to id of targetFolder as text

			set stageName to "create_receipt"
			return "OK" & tab & createdId & tab & targetFolderId
		end tell
	on error errorMessage number errorNumber
		return my errorEnvelope(stageName, errorNumber, errorMessage)
	end try
end createNote


on appendNote(accountName, folderName, shouldCreateFolder, noteIdentifier, htmlPath)
	set stageName to "resolve_account"
	try
		set htmlText to my readUtf8(htmlPath)
		tell application "Notes"
			set targetAccount to my resolveAccount(accountName)
			set stageName to "resolve_folder"
			set targetFolder to my resolveFolder(targetAccount, folderName, shouldCreateFolder)
			if shared of targetFolder then error "Target folder is shared" number -17005

			set stageName to "resolve_note"
			set targetNote to missing value
			repeat with candidateNote in notes of targetFolder
				if (id of candidateNote as text) is noteIdentifier then
					set targetNote to candidateNote
					exit repeat
				end if
			end repeat
			if targetNote is missing value then error "Apple Note not found" number -17003
			if password protected of targetNote then error "Password-protected note is not writable" number -17004
			if shared of targetNote then error "Shared note is not writable" number -17005

			set stageName to "append_note"
			set body of targetNote to ((body of targetNote as text) & htmlText)
			set targetFolderId to id of targetFolder as text

			set stageName to "append_receipt"
			return "OK" & tab & noteIdentifier & tab & targetFolderId
		end tell
	on error errorMessage number errorNumber
		return my errorEnvelope(stageName, errorNumber, errorMessage)
	end try
end appendNote


on findMarker(accountName, folderName, shouldCreateFolder, markerText)
	set stageName to "resolve_account"
	try
		if markerText is "" then error "Marker cannot be empty" number -17000
		tell application "Notes"
			set targetAccount to my resolveAccount(accountName)
			set stageName to "resolve_folder"
			set targetFolder to my resolveFolder(targetAccount, folderName, shouldCreateFolder)
			set stageName to "find_marker"
			set matchedIds to {}
			repeat with candidateNote in notes of targetFolder
				if (plaintext of candidateNote as text) contains markerText then
					set end of matchedIds to (id of candidateNote as text)
				end if
			end repeat
			if (count of matchedIds) is 0 then return "NOT_FOUND"
			if (count of matchedIds) is greater than 1 then return "CONFLICT"
			return "FOUND" & tab & (item 1 of matchedIds) & tab & (id of targetFolder as text)
		end tell
	on error errorMessage number errorNumber
		return my errorEnvelope(stageName, errorNumber, errorMessage)
	end try
end findMarker


on resolveAccount(accountName)
	tell application "Notes"
		if accountName is "default" then return default account
		set accountMatches to every account whose name is accountName
		if (count of accountMatches) is 0 then error "Apple Notes account not found" number -17001
		if (count of accountMatches) is greater than 1 then error "Apple Notes account is ambiguous" number -17006
		return item 1 of accountMatches
	end tell
end resolveAccount


on resolveFolder(targetAccount, folderName, shouldCreateFolder)
	tell application "Notes"
		set folderMatches to every folder of targetAccount whose name is folderName
		if (count of folderMatches) is greater than 1 then error "Apple Notes folder is ambiguous" number -17007
		if (count of folderMatches) is greater than 0 then return item 1 of folderMatches
		if not shouldCreateFolder then error "Apple Notes folder not found" number -17002
		return make new folder at targetAccount with properties {name:folderName}
	end tell
end resolveFolder


on readUtf8(filePath)
	if filePath is "" then error "HTML input is missing" number -17000
	set inputFile to POSIX file filePath
	return read inputFile as «class utf8»
end readUtf8


on errorEnvelope(stageName, errorNumber, errorMessage)
	return "ERROR" & tab & my cleanField(stageName) & tab & (errorNumber as text) & tab & my cleanField(errorMessage)
end errorEnvelope


on cleanField(inputValue)
	set cleanValue to inputValue as text
	set cleanValue to my replaceText(tab, " ", cleanValue)
	set cleanValue to my replaceText(return, " ", cleanValue)
	set cleanValue to my replaceText(linefeed, " ", cleanValue)
	return cleanValue
end cleanField


on replaceText(searchText, replacementText, inputText)
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to searchText
	set textItems to text items of inputText
	set AppleScript's text item delimiters to replacementText
	set outputText to textItems as text
	set AppleScript's text item delimiters to oldDelimiters
	return outputText
end replaceText
