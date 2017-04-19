from __future__ import print_function

import numpy as np
from numpy import zeros, ones, einsum

from .constants import tol6, tol8, tol12, Ha2eV, kb_HaK

from .mathutil import delta_lorentzian
from . import EigFile, Eigr2dFile, FanFile, DdbFile, GkkFile

__author__ = "Gabriel Antonius"

__all__ = ['QptAnalyzer']


class QptAnalyzer(object):

    def __init__(self,
                 ddb_fname=None,
                 eigq_fname=None,
                 eigk_fname=None,
                 eigr2d_fname=None,
                 eigr2d0_fname=None,
                 eigi2d_fname=None,
                 fan_fname=None,
                 fan0_fname=None,
                 gkk_fname=None,
                 gkk0_fname=None,
                 wtq=1.0,
                 smearing=0.00367,
                 temperatures=None,
                 omegase=None,
                 asr=True,
                 mu=None,
                 ):

        # Files
        self.ddb = DdbFile(ddb_fname, read=False, asr=asr)
        self.eigq = EigFile(eigq_fname, read=False)
        self.eigr2d = Eigr2dFile(eigr2d_fname, read=False)
        self.eigi2d = Eigr2dFile(eigi2d_fname, read=False)
        self.fan = FanFile(fan_fname, read=False)
        self.eig0 = EigFile(eigk_fname, read=False)
        self.eigr2d0 = Eigr2dFile(eigr2d0_fname, read=False)
        self.fan0 = FanFile(fan0_fname, read=False)
        self.gkk = GkkFile(gkk_fname, read=False)
        self.gkk0 = GkkFile(gkk0_fname, read=False)

        self.wtq = wtq
        self.smearing = smearing
        self.omegase = omegase if omegase else list()
        self.temperatures = temperatures if temperatures else list()
        self.mu = mu

    @property
    def nkpt(self):
        if self.eigr2d.fname:
            return self.eigr2d.nkpt
        elif self.fan.fname:
            return self.fan.nkpt
        elif self.gkk.fname:
            return self.gkk.nkpt
        else:
            raise Exception("Don't know nkpt. No files to read.")

    @property
    def nband(self):
        if self.eigr2d.fname:
            return self.eigr2d.nband
        elif self.fan.fname:
            return self.fan.nband
        elif self.gkk.fname:
            return self.gkk.nband
        else:
            raise Exception("Don't know nband. No files to read.")

    @property
    def natom(self):
        return self.ddb.natom

    @property
    def nmode(self):
        return self.ddb.nmode

    @property
    def is_gamma(self):
        return self.ddb.is_gamma

    @property
    def qred(self):
        return self.ddb.qred

    @property
    def omega(self):
        return self.ddb.omega

    @property
    def nomegase(self):
        return len(self.omegase)

    @property
    def ntemp(self):
        return len(self.temperatures)

    @property
    def use_gkk(self):
        return (bool(self.gkk.fname) and bool(self.gkk0.fname))

    @property
    def has_active(self):
        return (bool(self.fan.fname) and bool(self.fan0.fname)) or self.use_gkk

    def read_nonzero_files(self):
        """Read all nc files that are not specifically related to q=0."""
        for f in (self.ddb, self.eigq, self.eigr2d, self.eigi2d,
                  self.fan, self.gkk):
            if f.fname:
                f.read_nc()

        self.ddb.compute_dynmat()

    def read_ddb(self):
        """Read the ddb and diagonalize the matrix, setting omega."""
        self.ddb.read_nc()
        self.ddb.compute_dynmat()

    def read_zero_files(self):
        """Read all nc files that are not specifically related to q=0."""
        for f in (self.eig0, self.eigr2d0, self.fan0, self.gkk0):
            if f.fname:
                f.read_nc()

    def broadcast_zero_files(self):
        """Broadcast the data related to q=0 from master to all workers."""

        if self.eig0.fname:
            self.eig0.broadcast()
            self.eig0.get_degen()

        if self.eigr2d0.fname:
            self.eigr2d0.broadcast()

        if self.fan0.fname:
            self.fan0.broadcast()

        if self.gkk0.fname:
            self.gkk0.broadcast()

    def get_occ_nospin(self):
        """
        Get the occupations, being either 0 or 1, regardless of spinor.
        Assumes a gapped system, where occupations are the same at all kpts. 
        Returns: occ[nband]
        """
        if self.eigr2d.fname:
            occ = self.eigr2d.occ[0,0,:]
        elif self.fan.fname:
            occ = self.fan.occ[0,0,:]
        elif self.gkk.fname:
            occ = self.gkk.occ[0,0,:]
        else:
            raise Exception("Don't know nband. No files to read.")

        if any(occ == 2.0):
            occ = occ / 2.0

        return occ

    def get_max_val(self):
        """Get the maximum valence band energy."""
        occ0 = self.get_occ_nospin()
        eig = self.eigq.EIG[0,0,:]

        E_last = eig[0]
        for f, E in zip(occ0, eig):
            if f < 0.5:
                break
            E_last = E

        return E_last

    def get_min_cond(self):
        """Get the minimum conduction band energy."""
        occ0 = self.get_occ_nospin()
        eig = self.eigq.EIG[0,0,:]

        for f, E in zip(occ0, eig):
            if f <= 0.5:
                break

        return E

    def find_fermi_level(self):
        """
        Find the Fermi level locally, using the eigenvalues
        at all k+q points available. Assuming a gapped system.
        """
        return (self.get_max_val() + self.get_min_cond()) / 2.0

    @staticmethod
    def reduce_array(arr, mode=False, temperature=False, omega=False):
        """
        Eliminate dimensions from an array of shape
        (nmode, ntemp, nomegase, nkpt, nband)
        by summing over any or all of the first three dimension.

        mode:
            Keep the first dimension
        temperature:
            Keep the second dimension
        omega:
            Keep the third dimension
        """
        # Find the final order of 
        final_indices = ''
        if mode:
            final_indices += 'o'
        if temperature:
            final_indices += 't'
        if omega:
            final_indices += 'l'
        final_indices += 'kn'

        summation = 'otlkn->' + final_indices

        return einsum(summation, arr)

    def get_fan_ddw_sternheimer(self, mode=False, omega=False, temperature=False):
        """
        Compute the fan and ddw contribution to the self-energy
        obtained from the Sternheimer equation,
        that is, the contribution of the upper bands.

        Returns: fan, ddw

        The return arrays vary in dimensions, depending on the input arguments.
        These arrays are at most of dimension 5, as

            fan[nmode, ntemp, nomegase, nkpt, nband]
            ddw[nmode, ntemp, nomegase, nkpt, nband]

        Depending on the truth value of the input arguments,
        the dimension nomegase (omega) and ntemp (temperature)
        will be eliminated. 
        The dimension nmode will be summed over in case mode=False.

        In the semi-static approximation, these quantities do not actually
        depend on omega, so the arrays are simply repated along the omega axis.
        """

        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        nmode = self.nmode
        nomegase = self.nomegase
        ntemp = self.ntemp

        # Get reduced displacement (scaled with frequency)
        displ_red_FAN2, displ_red_DDW2 = self.ddb.get_reduced_displ_squared()
    
        bose = self.ddb.get_bose(self.temperatures)
    
        # FIXME this will not work for nsppol=2
        # nmode, nkpt, nband
        fan = einsum('knabij,objai->okn', self.eigr2d.EIG2D, displ_red_FAN2)
        ddw = einsum('knabij,objai->okn', self.eigr2d0.EIG2D, displ_red_DDW2)

        # Temperature dependence factor
        tdep = 2 * bose + 1 if temperature else ones((nmode,1))

        # Omega dependence factor
        odep = ones(nomegase) if omega else ones(1)

        # nmode, ntemp, nkpt, nband
        fan = einsum('okn,ot->otkn', fan, tdep)
        ddw = einsum('okn,ot->otkn', ddw, tdep)

        # nmode, ntemp, nomega, nkpt, nband
        fan = einsum('otkn,l->otlkn', fan, odep)
        ddw = einsum('otkn,l->otlkn', ddw, odep)

        # Reduce the arrays
        fan = self.reduce_array(fan, mode=mode, temperature=temperature, omega=omega)
        ddw = self.reduce_array(ddw, mode=mode, temperature=temperature, omega=omega)

        return fan, ddw
    
    def get_fan_ddw_gkk2_active(self):
        """
        Compute the squared gkk elements for the fan ddw terms.

        Returns:
            fan[nkpt, nband, nband, nmode]
            ddw[nkpt, nband, nband, nmode]
        """

        if not self.has_active:
            raise Exception('You should provide GKK files or FAN files '
                            'to compute active space contribution.')

        # Get reduced displacement (scaled with frequency)
        displ_red_FAN2, displ_red_DDW2 = self.ddb.get_reduced_displ_squared()

        if self.use_gkk:
            gkk2 = self.gkk.get_gkk_squared()
            gkk02 = self.gkk0.get_gkk_squared()
        else:
            gkk2 = self.fan.FAN
            gkk02 = self.fan0.FAN

        # nkpt, nband, nband, nmode
        fan = einsum('kniajbm,oabij->knmo', gkk2, displ_red_FAN2)
        ddw = einsum('kniajbm,oabij->knmo', gkk02, displ_red_DDW2)

        # Enforce the diagonal coupling terms to be zero at Gamma
        ddw = self.eig0.symmetrize_fan_degen(ddw)
        if self.is_gamma:
            fan = self.eig0.symmetrize_fan_degen(fan)
      
        return fan, ddw
    
    def get_fan_ddw_active(self, mode=False, omega=False, temperature=False, dynamical=True):
        """
        Compute the fan and ddw contributions to the self-energy
        from the active space, that is, the the lower bands.

        Returns: fan, ddw

        The return arrays vary in dimensions, depending on the input arguments.
        These arrays are at most of dimension 5, as

            fan[nmode, ntemp, nomegase, nkpt, nband]
            ddw[nmode, ntemp, nomegase, nkpt, nband]

        Depending on the truth value of the input arguments,
        the dimension nomegase (omega) and ntemp (temperature)
        will be eliminated. 
        The dimension nmode will be summed over in case mode=False.

        The Debye-Waller term does not actually depends on omega,
        but this dimension is kept anyway.
        """

        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        nmode = self.nmode

        if temperature:
            ntemp = self.ntemp
            temperatures = self.temperatures
        else:
            ntemp = 1
            temperatures = zeros(1)

        if omega:
            nomegase = self.nomegase
            omega_se = self.omegase
        else:
            # omega_se is measured from the bare eigenvalues
            nomegase = 1
            omega_se = zeros(1)

        if dynamical:
            omega_q = self.ddb.omega[:].real
        else:
            omega_q = zeros(nmode)

        # Bose-Enstein occupation number
        # ntemp
        bose = self.ddb.get_bose(temperatures)

        # Fermi-Dirac occupation number
        # nspin, nkpt, nband, ntemp
        occ = self.eigq.get_fermi_function(self.mu, temperatures)

        # nband
        occ0 = self.get_occ_nospin()
    
        # G^2
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()


        # DDW term
        # --------

        # nkpt, nband, nband
        delta_E_ddw = (einsum('kn,m->knm', self.eig0.EIG[0,:,:].real, ones(nband))
                     - einsum('kn,m->kmn', self.eig0.EIG[0,:,:].real, ones(nband))
                     - einsum('kn,m->knm', ones((nkpt,nband)), (2*occ0-1)) * self.smearing * 1j)

        # nkpt, nband, nmode
        ddw = einsum('knmo,knm->okn', ddw_num, 1.0 / delta_E_ddw)

        # ntemp, nkpt, nband

        tdep = 2 * bose + 1 if temperature else ones((nmode, ntemp))

        # FIXME This is not optimal: The mode indices will be summed
        #       so there is no need to create an array this big.
        #       in case omega=True and mode=False
        ddw = einsum('okn,ot->otkn', ddw, tdep)

        odep = ones(nomegase) if omega else ones(0)

        # ntemp, nomega, nkpt, nband
        ddw = einsum('otkn,l->otlkn', ddw, ones(nomegase))

        # Reduce the arrays
        ddw = self.reduce_array(ddw, mode=mode, temperature=temperature, omega=omega)


        # Fan term
        # --------

        # nomegase, ntemp, nkpt, nband
        fan = zeros((nmode, ntemp, nomegase, nkpt, nband), dtype=complex)

        if temperature:
            n_B = bose
        else:
            n_B = zeros((nmode,1))

        # nkpt, nband, nmode, ntemp
        # n + 1 - f
        num1 = (einsum('ot,kn->knot', n_B, ones((nkpt,nband)))
              + 1. - einsum('knt,o->knot', occ[0,:,:,:], ones(nmode)))

        # nkpt, nband, nmode, ntemp
        # n + f
        num2 = (einsum('ot,kn->knot', n_B, ones((nkpt,nband)))
              + einsum('knt,o->knot', occ[0,:,:,:], ones(nmode)))
    
        for kband in range(nband):
    
            # nkpt, nband
            # delta_E[ikpt,jband] = E[ikpt,jband] - E[ikpt,kband] - (2f[kband] -1) * eta * 1j
            delta_E = (self.eig0.EIG[0,:,:].real
                       - einsum('k,n->kn', self.eigq.EIG[0,:,kband].real, ones(nband))
                       - ones((nkpt,nband)) * (2*occ0[kband]-1) * self.smearing * 1j)
    
            # nkpt, nband, nomegase
            # delta_E_omega[ikpt,jband,lomega] = omega[lomega] + E[ikpt,jband] - E[ikpt,kband] - (2f[kband] -1) * eta * 1j
            delta_E_omega = (einsum('kn,l->knl', delta_E, ones(nomegase))
                           + einsum('kn,l->knl', ones((nkpt,nband)), omega_se))
    
            # nkpt, nband, nomegase, nmode
            deno1 = (einsum('knl,o->knlo', delta_E_omega, ones(3*natom))
                   - einsum('knl,o->knlo', ones((nkpt,nband,nomegase)), omega_q))

            # nmode, nkpt, nband, nomegase, ntemp
            div1 = einsum('knot,knlo->oknlt', num1, 1.0 / deno1)
    
            del deno1
    
            # nkpt, nband, nomegase, nmode
            deno2 = (einsum('knl,o->knlo', delta_E_omega, ones(3*natom))
                   + einsum('knl,o->knlo', ones((nkpt,nband,nomegase)), omega_q))
    
    
            # nmode, nkpt, nband, nomegase, ntemp
            div2 = einsum('knot,knlo->oknlt', num2, 1.0 / deno2)

            del deno2
    
            # FIXME This is not optimal: The mode indices will be summed
            #       so there is no need to create an array this big.
            # in case omega=True and mode=False

            # nmode, ntemp, nomegase, nkpt, nband
            fan += einsum('kno,oknlt->otlkn', fan_num[:,:,kband,:], div1 + div2)
    
            del div1, div2
      
        # Reduce the arrays
        fan = self.reduce_array(fan, mode=mode, temperature=temperature, omega=omega)

        return fan, ddw

    def get_fan_ddw(self, mode=False, temperature=False, omega=False, dynamical=False):

        fan_stern, ddw_stern = self.get_fan_ddw_sternheimer(mode=False, temperature=False, omega=False)
        fan_active, ddw_active = self.get_fan_ddw_active(mode=False, temperature=False, omega=False, dynamical=False)

        fan = fan_active + fan_stern
        ddw = ddw_active + ddw_stern

        return fan, ddw

    def get_zpr_static_sternheimer(self):
        """Compute the q-point zpr contribution in a static scheme."""
    
        # nkpt, nband
        fan, ddw = self.get_fan_ddw_sternheimer(mode=False, omega=False, temperature=False)
    
        self.zpr = self.wtq * (fan - ddw)
        self.zpr = self.eig0.make_average(self.zpr)
    
        return self.zpr

    def get_zpr_static(self):
        """
        Compute the q-point zpr contribution in a static scheme,
        with the transitions split between active and sternheimer.
        """

        # nkpt, nband
        fan, ddw = self.get_fan_ddw(mode=False, temperature=False, omega=False, dynamical=False)

        self.zpr = self.wtq * (fan - ddw).real
        self.zpr = self.eig0.make_average(self.zpr)
      
        return self.zpr

    def get_zpr_dynamical(self):
        """
        Compute the q-point zpr contribution in a static scheme
        with the transitions split between active and sternheimer.
        """

        # # nkpt, nband
        # fan, ddw = self.get_fan_ddw(mode=False, temperature=False, omega=False, dynamical=True)
        # self.zpr = self.wtq * (fan - ddw).real
        # self.zpr = self.eig0.make_average(self.zpr)
        # return self.zpr
    

        nkpt = self.eigr2d.nkpt
        nband = self.eigr2d.nband
        natom = self.eigr2d.natom
      
        self.zpr = zeros((nkpt, nband), dtype=complex)
      
        fan = zeros((nkpt, nband), dtype=complex)
        ddw = zeros((nkpt, nband), dtype=complex)
        fan_active  = zeros((nkpt, nband), dtype=complex)
        ddw_active  = zeros((nkpt, nband), dtype=complex)
      
        # Sternheimer contribution
        # ------------------------
      
        # nmode, nkpt, nband
        fan_stern, ddw_stern = self.get_fan_ddw_sternheimer(mode=False, temperature=False, omega=False)
      
        # Active space contribution
        # -------------------------
      
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
    
        # nband
        if any(self.eigr2d.occ[0,0,:] == 2.0):
            occ = self.eigr2d.occ[0,0,:]/2
        else:
            occ = self.eigr2d.occ[0,0,:]

        # DEBUG
        print(occ)
        # END DEBUG

        # nspin, nkpt, nband, ntemp
        # FIXME this is the source of the error.
        occ = self.eigq.get_fermi_function(self.mu, zeros(1))
        # nband
        occ = occ[0,0,:,0]

        # DEBUG
        #print(occ)
        #print(self.mu)
        #print(self.eigq.EIG[0,:,:])
        # END DEBUG
    
        #occ = self.eigq.get_fermi_function_T0(self.mu)
        #occ = einsum('skn,t->sknt', occ, ones(1))

        # nkpt, nband, nband
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband))
                 - einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband))
                 - einsum('ij,k->ijk', ones((nkpt,nband)), (2*occ-1)) * self.smearing * 1j)
    
        # nmode
        omega_q = self.ddb.omega[:].real
    
        # nband
        num1 = 1.0 - occ
        num2 = occ
    
        # nkpt, nband, nband, nmode
        deno1 = (einsum('knm,o->knmo', delta_E, ones(3*natom))
               - einsum('knm,o->knmo', ones((nkpt,nband,nband)), omega_q))
    
        # nmode, nband, nkpt, nband
        div1 = einsum('m,knmo->omkn', num1, 1.0 / deno1)
    
        # nkpt, nband, nband, nmode
        deno2 = (einsum('knm,o->knmo', delta_E, ones(3*natom))
               + einsum('knm,o->knmo', ones((nkpt,nband,nband)), omega_q))
    
        # nmode, nband, nkpt, nband
        div2 = einsum('m,knmo->omkn', num2, 1.0 / deno2)
    
        # nkpt, nband
        fan_active = einsum('knmo,omkn->kn', fan_num, div1 + div2)
    
        # FIXME I dont get the same result here....
        tmp_fan_active, ddw_active = self.get_fan_ddw_active(mode=False, temperature=False, omega=False, dynamical=True)

        # Summing Sternheimer and active space contributions
        # --------------------------------------------------

        fan = fan_stern + fan_active
        ddw = ddw_stern + ddw_active
    
        self.zpr = (fan - ddw) * self.wtq
    
        self.zpr = self.eig0.make_average(self.zpr)
      
        return self.zpr

    def get_zpb_dynamical(self):
        """
        Compute the zp broadening contribution from one q-point in a dynamical scheme.
        Only take the active space contribution.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
      
        # nband
        occ = self.get_occ_nospin()
    
        self.zpb = zeros((nkpt, nband), dtype=complex)
      
        # nmode
        omega = self.ddb.omega[:].real
    
        fan_add  = zeros((nkpt,nband), dtype=complex)
      
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
      
        # nkpt, nband, nband
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband))
                 - einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband)))
    
        # nband
        num1 = - (1. - occ) * (2 * occ - 1.)
        num2 = - occ * (2 * occ - 1.)
    
        # nkpt, nband, nband, nmode
        deno1 = (einsum('ijk,l->ijkl', delta_E, ones(3*natom))
               - einsum('ijk,l->ijkl', ones((nkpt,nband,nband)), omega))

        delta1 =  np.pi * delta_lorentzian(deno1, self.smearing)
    
        # nkpt, nband, nband, nmode
        deno2 = (einsum('ijk,l->ijkl', delta_E, ones(3*natom))
               + einsum('ijk,l->ijkl', ones((nkpt,nband,nband)), omega))

        delta2 = np.pi * delta_lorentzian(deno2, self.smearing)

        term1 = einsum('i,jkil->lijk', num1, delta1)
        term2 = einsum('i,jkil->lijk', num2, delta2)

        deltas = term1 + term2

        # nkpt, nband
        fan_add = einsum('ijkl,lkij->ij', fan_num, deltas)

        self.zpb = fan_add * self.wtq
        self.zpb = self.eig0.make_average(self.zpb)
      
        return self.zpb

    def get_zpb_static(self):
        """
        Compute the zp broadening contribution from one q-point in a static scheme.
        Only take the active space contribution.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
      
        # nband
        occ = self.get_occ_nospin()
    
        self.zpb = zeros((nkpt, nband), dtype=complex)
      
        # nmode
        omega = self.ddb.omega[:].real
      
        fan_add  = zeros((nkpt,nband), dtype=complex)
    
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
      
        # nkpt, nband, nband
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband))
                 - einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband)))
    
        # nband
        num = - (2 * occ - 1.)
    
        # nkpt, nband, nband, nmode
        delta =  np.pi * delta_lorentzian(delta_E, self.smearing)
    
        # nband, nkpt, nband
        deltasign = einsum('i,jki->ijk', num, delta)
    
        # nkpt, nband
        fan_add = einsum('ijkl,kij->ij', fan_num, deltasign)
      
        self.zpb = fan_add * self.wtq
        self.zpb = self.eig0.make_average(self.zpb)
      
        return self.zpb

    def get_zp_self_energy(self):
        """
        Compute the zp frequency-dependent dynamical self-energy from one q-point.
    
        The self-energy is evaluated on a frequency mesh 'omegase' that is shifted by the bare energies,
        such that, what is retured is
    
            Simga'_kn(omega) = Sigma_kn(omega + E^0_kn)

        Returns: sigma[nkpt,nband,nomegase]
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
    
        nomegase = self.nomegase
      
        self.sigma = zeros((nkpt, nband, nomegase), dtype=complex)
      
        # nmode
        omega = self.ddb.omega[:].real
    
        fan = zeros((nomegase, nkpt, nband), dtype=complex)
        ddw = zeros((nkpt, nband), dtype=complex)
        fan_add  = zeros((nomegase, nkpt,nband), dtype=complex)
        ddw_add  = zeros((nkpt, nband), dtype=complex)
      
        # Sternheimer contribution
        # ------------------------
      
        fan, ddw = self.get_fan_ddw_sternheimer(mode=False, temperature=False, omega=True)
      
        # Active space contribution
        # -------------------------
      
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
    
        # nkpt, nband, nband
        ddw_tmp = np.sum(ddw_num, axis=3)
    
        # nband
        occ = self.get_occ_nospin()
    
        # nkpt, nband, nband
        delta_E_ddw = (einsum('kn,m->knm', self.eig0.EIG[0,:,:].real, ones(nband))
                     - einsum('kn,m->kmn', self.eig0.EIG[0,:,:].real, ones(nband))
                     - einsum('kn,m->knm', ones((nkpt,nband)), (2*occ-1)) * self.smearing * 1j)
    
        # nkpt, nband
        ddw_add = einsum('knm,knm->kn', ddw_tmp, 1.0 / delta_E_ddw)
        ddw_add = einsum('kn,l->lkn', ddw_add, ones(nomegase))
    
        # nband
        num1 = 1.0 - occ
    
        # nomegase, nkpt, nband
        fan_add = zeros((nomegase, nkpt, nband), dtype=complex)
    
        for kband in range(nband):
    
            # nkpt, nband
            # delta_E[ikpt,jband] = E[ikpt,jband] - E[ikpt,kband] - (2f[kband] -1) * eta * 1j
            delta_E = (self.eig0.EIG[0,:,:].real
                     - einsum('k,n->kn', self.eigq.EIG[0,:,kband].real, ones(nband))
                     - ones((nkpt,nband)) * (2*occ[kband]-1) * self.smearing * 1j)
    
            # nkpt, nband, nomegase
            # delta_E_omega[ikpt,jband,lomega] = omega[lomega] + E[ikpt,jband] - E[ikpt,kband] - (2f[kband] -1) * eta * 1j
            delta_E_omega = (einsum('kn,l->knl', delta_E, ones(nomegase))
                           + einsum('kn,l->knl', ones((nkpt,nband)), self.omegase))
    
            # nkpt, nband, nomegase, nmode
            deno1 = (einsum('knl,o->knlo', delta_E_omega, ones(3*natom))
                   - einsum('knl,o->knlo', ones((nkpt,nband,nomegase)), omega))
    
            # nmode, nkpt, nband, nomegase
            div1 = num1[kband] * einsum('knlo->oknl', 1.0 / deno1)
    
            del deno1
    
            # nkpt, nband, nomegase, nmode
            deno2 = (einsum('knl,o->knlo', delta_E_omega, ones(3*natom))
                   + einsum('knl,o->knlo', ones((nkpt,nband,nomegase)), omega))
    
            del delta_E_omega
    
            # nmode, nkpt, nband, nomegase
            div2 = occ[kband] * einsum('knlo->oknl', 1.0 / deno2)
    
            del deno2
    
            # nomegase, nkpt, nband
            fan_add += einsum('kno,oknl->lkn', fan_num[:,:,kband,:], div1 + div2)
    
            del div1, div2
      
    
        # Summing Sternheimer and active space contributions
        # --------------------------------------------------
      

        fan += fan_add
        ddw += ddw_add
    
        self.sigma = (fan - ddw) * self.wtq
    
        self.sigma = self.eig0.make_average(self.sigma)
        self.sigma = einsum('lkn->knl', self.sigma)
      
        return self.sigma

    def get_td_self_energy(self):
        """
        Compute the temperature depended and frequency-dependent dynamical self-energy from one q-point.
    
        The self-energy is evaluated on a frequency mesh 'omegase' that is shifted by the bare energies,
        such that, what is retured is
    
            Simga'_kn(omega,T) = Sigma_kn(omega + E^0_kn, T)
    
        Returns: sigma[nkpt,nband,nomegase,ntemp]
        """
    
        # ntemp, nomegase, nkpt, nband
        fan_stern,  ddw_stern  = self.get_fan_ddw_sternheimer(mode=False, temperature=True, omega=True)
        fan_active, ddw_active = self.get_fan_ddw_active(mode=False, omega=True, temperature=True)
      
        self.sigma =  self.wtq * (
            (fan_active - ddw_active) + (fan_stern - ddw_stern))

        self.sigma = self.eig0.make_average(self.sigma)

        # nkpt, nband, nomegase, nband
        self.sigma = einsum('tlkn->knlt', self.sigma)
      
        return self.sigma

    def get_tdr_static(self):
        """
        Compute the q-point contribution to the temperature-dependent
        renormalization in a static scheme,
        with the transitions split between active and sternheimer.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        ntemp = self.ntemp
    
        # These indicies be swapped at the end
        self.tdr = zeros((ntemp, nkpt, nband), dtype=complex)
    
        bose = self.ddb.get_bose(self.temperatures)
    
        fan =  zeros((ntemp, nkpt, nband), dtype=complex)
        ddw = zeros((ntemp, nkpt, nband), dtype=complex)
        fan_add = zeros((ntemp, nkpt, nband),dtype=complex)
        ddw_add = zeros((ntemp, nkpt, nband),dtype=complex)
    
        # Sternheimer contribution
        # ------------------------
      
        # ntemp, nkpt, nband
        fan, ddw = self.get_fan_ddw_sternheimer(mode=False, temperature=True, omega=False)
      
        # Active space contribution
        # ------------------------

        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
    
        # ikpt,iband,jband      
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband)) -
                   einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband)))
    
        delta_E_ddw = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband)) -
                       einsum('ij,k->ikj', self.eig0.EIG[0,:,:].real, ones(nband)))
    
        # imode,ntemp,ikpt,iband,jband
        num = einsum('ij,klm->ijklm', 2*bose+1., delta_E)
    
        # ikpt,iband,jband
        deno = delta_E ** 2 + self.smearing ** 2
    
        # imode,ntemp,ikpt,iband,jband 
        div =  einsum('ijklm,klm->ijklm', num, 1. / deno)
    
        #(ikpt,iband,jband,imode),(imode,ntemp,ikpt,iband,jband)->ntemp,ikpt,iband
        fan_add = einsum('ijkl,lmijk->mij', fan_num, div)
    
        # imode,ntemp,ikpt,iband,jband
        num = einsum('ij,klm->ijklm', 2*bose+1., delta_E_ddw)
    
        # ikpt,iband,jband
        deno = delta_E_ddw ** 2 + self.smearing ** 2
    
        div =  einsum('ijklm,klm->ijklm', num, 1. / deno)
    
        #(ikpt,iband,jband,imode),(imode,ntemp,ikpt,iband,jband)->ntemp,ikpt,iband 
        ddw_add = einsum('ijkl,lmijk->mij', ddw_num, div)
    
    
        fan += fan_add
        ddw += ddw_add
    
        self.tdr = (fan - ddw) * self.wtq
    
        self.tdr = self.eig0.make_average(self.tdr)
    
        # nkpt, nband, ntemp
        self.tdr = np.einsum('kij->ijk', self.tdr)
    
        return self.tdr

    def get_tdr_dynamical(self):
        """
        Compute the q-point contribution to the temperature-dependent
        renormalization in a dynamical scheme.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        ntemp = self.ntemp
    
        self.tdr =  zeros((ntemp, nkpt, nband), dtype=complex)
    
        bose = self.ddb.get_bose(self.temperatures)
    
        fan =  zeros((ntemp, nkpt, nband), dtype=complex)
        ddw = zeros((ntemp, nkpt, nband), dtype=complex)
        fan_add = zeros((ntemp, nkpt, nband),dtype=complex)
        ddw_add = zeros((ntemp, nkpt, nband),dtype=complex)
    
        # Sternheimer contribution
        # ------------------------
      
        # ntemp, nkpt, nband
        fan, ddw = self.get_fan_ddw_sternheimer(mode=False, temperature=True, omega=False)
      
        # Active space contribution
        # -------------------------

        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()
    
        # jband
        occ = self.get_occ_nospin()
    
        delta_E_ddw = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband)) -
                       einsum('ij,k->ikj', self.eig0.EIG[0,:,:].real, ones(nband)) -
                       einsum('ij,k->ijk', ones((nkpt, nband)), (2*occ-1)) * self.smearing * 1j)
    
        # ntemp,ikpt,iband,jband
        tmp = einsum('ijkl,lm->mijk', ddw_num, 2*bose+1.0)
        ddw_add = einsum('ijkl,jkl->ijk', tmp, 1.0 / delta_E_ddw)
    
        # ikpt,iband,jband
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband)) -
                   einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband)) -
                   einsum('ij,k->ijk', ones((nkpt,nband)), (2*occ-1)) * self.smearing * 1j)
    
        omega = self.ddb.omega[:].real # imode
    
        # imode,ntemp,jband
        num1 = (einsum('ij,k->ijk', bose, ones(nband)) + 1.0 -
                einsum('ij,k->ijk', ones((3*natom, ntemp)), occ))
    
        # ikpt,iband,jband,imode
        deno1 = (einsum('ijk,l->ijkl', delta_E,ones(3*natom)) -
                 einsum('ijk,l->ijkl', ones((nkpt, nband, nband)), omega))
    
        # (imode,ntemp,jband)/(ikpt,iband,jband,imode) ==> imode,ntemp,jband,ikpt,iband
        invdeno1 = np.real(deno1) / (np.real(deno1) ** 2 + np.imag(deno1) ** 2)
        div1 = einsum('ijk,lmki->ijklm', num1, invdeno1)
        #div1 = einsum('ijk,lmki->ijklm', num1, 1.0 / deno1)
    
        # imode,ntemp,jband
        num2 = (einsum('ij,k->ijk', bose, ones(nband)) +
                einsum('ij,k->ijk', ones((3*natom, ntemp)), occ))
    
        # ikpt,iband,jband,imode
        deno2 = (einsum('ijk,l->ijkl', delta_E, ones(3*natom)) +
                 einsum('ijk,l->ijkl', ones((nkpt, nband, nband)), omega))
    
        # (imode,ntemp,jband)/(ikpt,iband,jband,imode) ==> imode,ntemp,jband,ikpt,iband
        invdeno2 = np.real(deno2) / (np.real(deno2) ** 2 + np.imag(deno2) ** 2)
        div2 = einsum('ijk,lmki->ijklm', num2, invdeno2)
        #div2 = einsum('ijk,lmki->ijklm', num2, 1.0 / deno2)
    
        # ikpt,iband,jband,imode
        fan_add = einsum('ijkl,lmkij->mij', fan_num, div1 + div2)
    

        fan += fan_add
        ddw += ddw_add
    
        self.tdr = (fan - ddw) * self.wtq
    
        self.tdr = self.eig0.make_average(self.tdr)
    
        # nkpt, nband, ntemp
        self.tdr = np.einsum('kij->ijk', self.tdr)
    
        return self.tdr

    def get_tdb_static(self):
        """
        Compute the q-point contribution to the temperature-dependent broadening
        in a static scheme from the EIGI2D files.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        ntemp = self.ntemp
          
        # These indicies be swapped at the end
        self.tdb = zeros((ntemp, nkpt, nband), dtype=complex)
    
        # Get reduced displacement (scaled with frequency)
        displ_red_FAN2, displ_red_DDW2 = self.ddb.get_reduced_displ_squared()
    
        bose = self.ddb.get_bose(self.temperatures)
    
        fan_corrQ = einsum('ijklmn,olnkm->oij', self.eigi2d.EIG2D, displ_red_FAN2)
    
        for imode in np.arange(3*natom):
          for tt, T in enumerate(self.temperatures):
            self.tdb[tt,:,:] += np.pi * fan_corrQ[imode,:,:] * (2*bose[imode,tt] + 1.)
    
        self.tdb = self.tdb * self.wtq
    
        self.tdb = self.eig0.make_average(self.tdb)
    
        # nkpt, nband, ntemp
        self.tdb = np.einsum('kij->ijk', self.tdb)

        return self.tdb

    def get_zpr_static_modes(self):
        """
        Compute the q-point zpr contribution in a static scheme,
        with the transitions split between active and sternheimer.
        Retain the mode decomposition of the zpr.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
        nmode = 3 * natom
      
        self.zpr = zeros((nmode, nkpt, nband), dtype=complex)
      
        fan = zeros((nmode, nkpt, nband), dtype=complex)
        ddw = zeros((nmode, nkpt, nband), dtype=complex)
        fan_active  = zeros((nmode, nkpt, nband), dtype=complex)
        ddw_active  = zeros((nmode, nkpt, nband), dtype=complex)
      
        # Sternheimer contribution
        # ------------------------

        # nmode, nkpt, nband
        fan, ddw = self.get_fan_ddw_sternheimer(mode=True, temperature=False, omega=False)
      
        # Active space contribution
        # -------------------------
      
        # nkpt, nband, nband, nmode
        fan_num, ddw_num = self.get_fan_ddw_gkk2_active()

        # nmode, nkpt, nband, nband
        fan_tmp = einsum('ijkl->lijk', fan_num)
        ddw_tmp = einsum('ijkl->lijk', ddw_num)
        #fan_tmp = np.sum(fan_num, axis=3)
        #ddw_tmp = np.sum(ddw_num, axis=3)
      
        # nkpt, nband, nband
        delta_E = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband))
                 - einsum('ij,k->ikj', self.eigq.EIG[0,:,:].real, ones(nband)))
    
        # nkpt, nband, nband
        delta_E_ddw = (einsum('ij,k->ijk', self.eig0.EIG[0,:,:].real, ones(nband))
                     - einsum('ij,k->ikj', self.eig0.EIG[0,:,:].real, ones(nband)))
    
        # nkpt, nband, nband
        div =  delta_E / (delta_E ** 2 + self.smearing ** 2)
    
        # nmode, nkpt, nband
        fan_active = einsum('lijk,ijk->lij', fan_tmp, div)
    
        # nkpt, nband, nband
        div_ddw = delta_E_ddw / (delta_E_ddw ** 2 + self.smearing ** 2)
    
        # nmode, nkpt, nband
        ddw_active = einsum('lijk,ijk->lij', ddw_tmp, div_ddw)
    
      
        # Correction from active space 
        fan += fan_active
        ddw += ddw_active
    
        self.zpr = (fan - ddw) * self.wtq
    
        self.zpr = self.eig0.make_average(self.zpr)
      
        return self.zpr

    def get_zpb_static_nosplit(self):
        """
        Compute the zp broadening contribution from one q-point in a static scheme
        from the EIGI2D files.
        """
    
        nkpt = self.nkpt
        nband = self.nband
        natom = self.natom
    
        self.zpb = zeros((nkpt, nband), dtype=complex)
    
        # Get reduced displacement (scaled with frequency)
        displ_red_FAN2, displ_red_DDW2 = self.ddb.get_reduced_displ_squared()
        
        fan_corrQ = einsum('ijklmn,olnkm->oij', self.eigi2d.EIG2D, displ_red_FAN2)
    
        self.zpb += np.pi * np.sum(fan_corrQ, axis=0)
        self.zpb = self.zpb * self.wtq
    
        if np.any(self.zpb[:,:].imag > tol12):
          warnings.warn("The real part of the broadening is non zero: {}".format(broadening))
    
        self.zpb = self.eig0.make_average(self.zpb)
    
        return self.zpb

    def get_tdr_static_nosplit(self):
        """
        Compute the q-point contribution to the temperature-dependent
        renormalization in a static scheme.
        """
    
        # ntemp, nkpt, nband
        fan, ddw = self.get_fan_ddw_sternheimer(mode=False, temperature=True, omega=False)
    
        self.tdr = (fan - ddw) * self.wtq
    
        self.tdr = self.eig0.make_average(self.tdr)

        # nkpt, nband, ntemp
        self.tdr = np.einsum('tkn->knt', self.tdr)
    
        return self.tdr

