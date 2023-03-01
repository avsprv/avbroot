from . import external

import argparse
import concurrent.futures
import copy
import os
import shutil
import tempfile
import time
import zipfile

import avbtool

from . import boot
from . import openssl
from . import ota
from . import util
from . import vbmeta


PATH_METADATA = 'META-INF/com/android/metadata'
PATH_METADATA_PB = f'{PATH_METADATA}.pb'
PATH_OTACERT = 'META-INF/com/android/otacert'
PATH_PAYLOAD = 'payload.bin'
PATH_PROPERTIES = 'payload_properties.txt'

# Half-open range
MAGISK_MIN_VERSION = 22000
MAGISK_MAX_VERSION = 25300

PARTITION_PRIORITIES = {
    '@vbmeta': ('vbmeta',),
    # The kernel is always in boot
    '@gki_kernel': ('boot',),
    # Devices launching with Android 13 use a GKI init_boot ramdisk
    '@gki_ramdisk': ('init_boot', 'boot'),
    # OnePlus devices have a recovery image
    '@otacerts': ('recovery', 'vendor_boot', 'boot'),
}


def print_status(*args, **kwargs):
    print('\x1b[1m*****', *args, '*****\x1b[0m', **kwargs)


def print_warning(*args, **kwargs):
    print('\x1b[1;31m*****', '[WARNING]', *args, '*****\x1b[0m', **kwargs)


def get_partitions_by_type(manifest):
    all_partitions = set(p.partition_name for p in manifest.partitions)
    by_type = {}

    for t, candidates in PARTITION_PRIORITIES.items():
        partition = next((p for p in candidates if p in all_partitions), None)
        if partition is None:
            raise ValueError(f'Cannot find partition of type: {t}')

        by_type[t] = partition

    return by_type


def get_required_images(manifest, boot_partition):
    all_partitions = set(p.partition_name for p in manifest.partitions)
    by_type = get_partitions_by_type(manifest)
    images = {k: v for k, v in by_type.items()
              if k in {'@otacerts', '@vbmeta'}}

    if boot_partition in by_type:
        images['@rootpatch'] = by_type[boot_partition]
    elif boot_partition in all_partitions:
        images['@rootpatch'] = boot_partition
    else:
        raise ValueError(f'Boot partition not found: {boot_partition}')

    return images


def patch_ota_payload(f_in, open_more_f_in, f_out, file_size, boot_partition,
                      magisk, prepatched, clear_vbmeta_flags, privkey_avb,
                      passphrase_avb, privkey_ota, passphrase_ota, cert_ota):
    with tempfile.TemporaryDirectory() as temp_dir:
        extract_dir = os.path.join(temp_dir, 'extract')
        patch_dir = os.path.join(temp_dir, 'patch')
        payload_dir = os.path.join(temp_dir, 'payload')
        os.mkdir(extract_dir)
        os.mkdir(patch_dir)
        os.mkdir(payload_dir)

        version, manifest, blob_offset = ota.parse_payload(f_in)
        images = get_required_images(manifest, boot_partition)
        unique_images = set(images.values())

        print_status('Extracting', ', '.join(sorted(unique_images)),
                     'from the payload')
        ota.extract_images(open_more_f_in, manifest, blob_offset,
                           extract_dir, unique_images)

        image_patches = {}

        if magisk is not None:
            image_patches[images['@rootpatch']] = \
                [boot.MagiskRootPatch(magisk)]
        else:
            image_patches[images['@rootpatch']] = \
                [boot.PrepatchedImage(prepatched)]

        image_patches.setdefault(images['@otacerts'], []).append(
            boot.OtaCertPatch(cert_ota))

        avb = avbtool.Avb()

        print_status('Patching', ', '.join(sorted(image_patches)))
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(image_patches)) as executor:
            def apply_patches(image, patches):
                boot.patch_boot(
                    avb,
                    os.path.join(extract_dir, f'{image}.img'),
                    os.path.join(patch_dir, f'{image}.img'),
                    privkey_avb,
                    passphrase_avb,
                    True,
                    patches,
                )

            futures = [executor.submit(apply_patches, i, p)
                       for i, p in image_patches.items()]

            for future in concurrent.futures.as_completed(futures):
                future.result()

        print_status('Building new root vbmeta image')
        vbmeta_image = images['@vbmeta']
        vbmeta.patch_vbmeta_root(
            avb,
            [os.path.join(patch_dir, f'{i}.img')
                for i in unique_images if i != vbmeta_image],
            os.path.join(extract_dir, f'{vbmeta_image}.img'),
            os.path.join(patch_dir, f'{vbmeta_image}.img'),
            privkey_avb,
            passphrase_avb,
            manifest.block_size,
            clear_vbmeta_flags,
        )

        print_status('Updating OTA payload to reference patched images')
        return ota.patch_payload(
            f_in,
            f_out,
            version,
            manifest,
            blob_offset,
            payload_dir,
            {i: os.path.join(patch_dir, f'{i}.img') for i in unique_images},
            file_size,
            privkey_ota,
            passphrase_ota,
        )


def patch_ota_zip(f_zip_in, f_zip_out, boot_partition, magisk, prepatched,
                  clear_vbmeta_flags, privkey_avb, passphrase_avb, privkey_ota,
                  passphrase_ota, cert_ota):
    with (
        zipfile.ZipFile(f_zip_in, 'r') as z_in,
        zipfile.ZipFile(f_zip_out, 'w') as z_out,
    ):
        infolist = z_in.infolist()
        missing = {
            PATH_METADATA,
            PATH_METADATA_PB,
            PATH_OTACERT,
            PATH_PAYLOAD,
            PATH_PROPERTIES,
        }
        i_payload = -1
        i_properties = -1

        for i, info in enumerate(infolist):
            if info.filename in missing:
                missing.remove(info.filename)

            if info.filename == PATH_PAYLOAD:
                i_payload = i
            elif info.filename == PATH_PROPERTIES:
                i_properties = i

            if not missing and i_payload >= 0 and i_properties >= 0:
                break

        if missing:
            raise Exception(f'Missing files in zip: {missing}')

        # Ensure payload is processed before properties
        if i_payload > i_properties:
            infolist[i_payload], infolist[i_properties] = \
                infolist[i_properties], infolist[i_payload]

        properties = None
        metadata_info = None
        metadata_pb_info = None
        metadata_pb_raw = None

        for info in infolist:
            out_info = copy.copy(info)

            # Ignore because the plain-text legacy metadata file is regenerated
            # from the new metadata
            if info.filename == PATH_METADATA:
                metadata_info = info
                continue

            # The existing metadata is needed to generate a new signed zip
            elif info.filename == PATH_METADATA_PB:
                metadata_pb_info = info

                with z_in.open(info, 'r') as f_in:
                    metadata_pb_raw = f_in.read()

                continue

            # Use the user's OTA certificate
            elif info.filename == PATH_OTACERT:
                print_status('Replacing', info.filename)

                with (
                    open(cert_ota, 'rb') as f_cert,
                    z_out.open(out_info, 'w') as f_out,
                ):
                    shutil.copyfileobj(f_cert, f_out)

                continue

            # Copy other files, patching if needed
            with (
                z_in.open(info, 'r') as f_in,
                z_out.open(out_info, 'w') as f_out,
            ):
                if info.filename == PATH_PAYLOAD:
                    print_status('Patching', info.filename)

                    if info.compress_type != zipfile.ZIP_STORED:
                        raise Exception(
                            f'{info.filename} is not stored uncompressed')

                    properties = patch_ota_payload(
                        f_in,
                        lambda: z_in.open(info, 'r'),
                        f_out,
                        info.file_size,
                        boot_partition,
                        magisk,
                        prepatched,
                        clear_vbmeta_flags,
                        privkey_avb,
                        passphrase_avb,
                        privkey_ota,
                        passphrase_ota,
                        cert_ota,
                    )

                elif info.filename == PATH_PROPERTIES:
                    print_status('Patching', info.filename)

                    if info.compress_type != zipfile.ZIP_STORED:
                        raise Exception(
                            f'{info.filename} is not stored uncompressed')

                    f_out.write(properties)

                else:
                    print_status('Copying', info.filename)

                    shutil.copyfileobj(f_in, f_out)

        print_status('Generating', PATH_METADATA, 'and', PATH_METADATA_PB)
        metadata = ota.add_metadata(
            z_out,
            metadata_info,
            metadata_pb_info,
            metadata_pb_raw,
        )

        # Signing process needs to capture the zip central directory
        f_zip_out.start_capture()

        return metadata


def get_magisk_version(magisk):
    with zipfile.ZipFile(magisk, 'r') as z:
        with z.open('assets/util_functions.sh', 'r') as f:
            for line in f:
                if line.startswith(b'MAGISK_VER_CODE='):
                    return int(line[16:].strip())

    raise Exception(f'Failed to get Magisk version from: {magisk}')


def patch_subcommand(args):
    output = args.output
    if output is None:
        output = args.input + '.patched'

    if args.magisk is not None:
        magisk_version = get_magisk_version(args.magisk)
        if magisk_version < MAGISK_MIN_VERSION or \
                magisk_version >= MAGISK_MAX_VERSION:
            message = f'Unsupported Magisk version {magisk_version} ' \
                    f'(supported: >={MAGISK_MIN_VERSION}, ' \
                    f'<{MAGISK_MAX_VERSION})'

            if args.ignore_magisk_version:
                print_warning(message)
            else:
                raise Exception(message)

    # Get passphrases for keys
    passphrase_avb = openssl.prompt_passphrase(args.privkey_avb)
    passphrase_ota = openssl.prompt_passphrase(args.privkey_ota)

    # Ensure that the certificate matches the private key
    if not openssl.cert_matches_key(args.cert_ota, args.privkey_ota,
                                    passphrase_ota):
        raise Exception('OTA certificate does not match private key')

    start = time.perf_counter_ns()

    with util.open_output_file(output) as temp_raw:
        with (
            ota.open_signing_wrapper(temp_raw, args.privkey_ota,
                                     passphrase_ota, args.cert_ota) as temp,
            ota.match_android_zip64_limit(),
        ):
            metadata = patch_ota_zip(
                args.input,
                temp,
                args.boot_partition,
                args.magisk,
                args.prepatched,
                args.clear_vbmeta_flags,
                args.privkey_avb,
                passphrase_avb,
                args.privkey_ota,
                passphrase_ota,
                args.cert_ota,
            )

        # We do a lot of low-level hackery. Reopen and verify offsets
        print_status('Verifying metadata offsets')
        with zipfile.ZipFile(temp_raw, 'r') as z:
            ota.verify_metadata(z, metadata)

    # Excluding the time it takes for the user to type in the passwords
    elapsed = time.perf_counter_ns() - start
    print_status(f'Completed after {elapsed / 1_000_000_000:.1f}s')


def extract_subcommand(args):
    with zipfile.ZipFile(args.input, 'r') as z:
        info = z.getinfo(PATH_PAYLOAD)

        with z.open(info, 'r') as f:
            _, manifest, blob_offset = ota.parse_payload(f)

        if args.all:
            unique_images = set(p.partition_name
                                for p in manifest.partitions)
        else:
            images = get_required_images(manifest, args.boot_partition)
            unique_images = set(images.values())

        print_status('Extracting', ', '.join(sorted(unique_images)),
                     'from the payload')
        os.makedirs(args.directory, exist_ok=True)

        # Extract in parallel. There's is no actual I/O parallelism due to
        # zipfile's internal locks, but this is still significantly faster than
        # doing it single threaded. The extraction process is mostly CPU board
        # due to decompression.
        ota.extract_images(lambda: z.open(info, 'r'),
                           manifest, blob_offset, args.directory,
                           unique_images)


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='subcommand', required=True,
                                       help='Subcommands')

    patch = subparsers.add_parser('patch', help='Patch a full OTA zip')

    patch.add_argument('--input', required=True,
                       help='Path to original raw payload or OTA zip')
    patch.add_argument('--output',
                       help='Path to new raw payload or OTA zip')
    patch.add_argument('--privkey-avb', required=True,
                       help='Private key for signing root vbmeta image')
    patch.add_argument('--privkey-ota', required=True,
                       help='Private key for signing OTA payload')
    patch.add_argument('--cert-ota', required=True,
                       help='Certificate for OTA payload signing key')

    # The user can either allow us to patch or supply their own prepatched boot
    # image (eg. by either Magisk or KernelSU)
    boot_group = patch.add_mutually_exclusive_group(required=True)
    boot_group.add_argument('--magisk',
                            help='Path to Magisk APK')
    boot_group.add_argument('--prepatched',
                            help='Path to prepatched boot image')

    patch.add_argument('--ignore-magisk-version', action='store_true',
                       help='Allow patching with unsupported Magisk versions')

    patch.add_argument('--clear-vbmeta-flags', action='store_true',
                       help='Forcibly clear vbmeta flags if they disable AVB')

    extract = subparsers.add_parser(
        'extract', help='Extract patched images from a patched OTA zip')

    extract.add_argument('--input', required=True,
                         help='Path to patched OTA zip')
    extract.add_argument('--directory', default='.',
                         help='Output directory for extracted images')
    extract.add_argument('--all', action='store_true',
                         help='Extract all images from payload')

    for subcmd in (patch, extract):
        subcmd.add_argument('--boot-partition', default='@gki_ramdisk',
                            help='Boot partition name')

    args = parser.parse_args(args=argv)

    if args.subcommand == 'patch' and \
            args.ignore_magisk_version and args.magisk is None:
        parser.error('--ignore-magisk-version requires --magisk')

    return args


def main(argv=None):
    args = parse_args(argv=argv)

    if args.subcommand == 'patch':
        patch_subcommand(args)
    elif args.subcommand == 'extract':
        extract_subcommand(args)
    else:
        raise NotImplementedError()